"""Host identity: the keypair, the fingerprint, the signer, the routes.

THIS FILE IS ONE OF EXACTLY TWO in the repo allowed to import
`cryptography` -- convoy_hostkeys.py is the other, and
test_import_isolation_no_other_module_imports_cryptography below is what
holds that line. The imports here exist so the tests can act as an
ATTACKER would (sign raw TLS-shaped bytes, load the key file as an
outsider) instead of only through the API under test, which would make
the domain-separation test circular.
"""

import ast
import datetime
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import textwrap
import threading
import time
import warnings

import pytest

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

import convoy_hostapp as ha
import convoy_hostkeys as hk
import convoy_platform as cp
import convoy_protocol as protocol

from test_convoy_hostapp import Server            # noqa: F401 -- fixture use

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root():
    """The checkout root, found by MARKERS rather than a fixed number of
    dirname() calls.

    dirname(dirname(HERE)) assumed this file sits exactly two levels
    below the root, so the isolation tests below FAILED outright in a
    partial copy of dev/convoy -- which is how the review panel ended up
    deselecting them, losing their signal for a whole pass. Walking up
    for a directory that has both pytest.ini and dev/ works from a
    normal checkout, a git worktree, a submodule, or any relocation;
    when there is genuinely no checkout around (a bare copy of this
    directory), the tests SKIP with a named reason instead of failing,
    because "this guard did not run here" is true and "it passed" would
    not be.
    """
    directory = HERE
    while True:
        if (os.path.isfile(os.path.join(directory, "pytest.ini"))
                and os.path.isdir(os.path.join(directory, "dev"))):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


REPO = _find_repo_root()


@pytest.fixture
def data_dir(tmp_path):
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


def _envelope(signer, **over):
    fields = dict(convoy_id="studio", origin_host_id="h" * 32,
                  controller_id="ctl-1", target_node_id="n" * 32,
                  operation="convoy_ping", signer=signer,
                  arguments={"a": 1}, timeout_s=60.0)
    fields.update(over)
    return protocol.build_envelope(**fields)


def _verify(envelope, signer, **over):
    kwargs = dict(convoy_id="studio", my_node_id="n" * 32)
    kwargs.update(over)
    protocol.verify_envelope(envelope, signer, **kwargs)


# -- known-answer vectors ------------------------------------------------
#
# THE WIRE CONTRACT, pinned to fixed bytes. Everything else in this file
# tests a PROPERTY (stable across restart, unstable across rotation,
# derived from the SPKI and not the cert) -- and a property test cannot
# see a change to the DERIVATION ITSELF. Salt the hash, take 19 bytes
# instead of 20, swap the base32 alphabet, or change the domain tag, and
# every property still holds while two Convoy hosts on adjacent builds
# compute different fingerprints for the same key and every pin in the
# fleet mismatches for reasons nobody can diagnose. (Found by mutation:
# appending one byte before the SHA-256 left the whole suite green.)
#
# Ed25519 signing is deterministic (RFC 8032), so the signature vector
# pins the domain tag, the payload framing and the hex encoding at once.
# If one of these fails, you have changed the format -- that is a
# deliberate, coordinated, fleet-wide break, not a refactor.

_KAT_SEED = bytes(range(32))
_KAT_SPKI_DER = bytes.fromhex(
    "302a300506032b657003210003a107bff3ce10be1d70dd18e74bc09967e4d630"
    "9ba50d5f1ddc8664125531b8")
_KAT_FINGERPRINT = "cvfp1-m188-6zc5-0w2r-5k7q-755g-k244-fk1h-5jw8"
_KAT_PAYLOAD = b'{"convoy_id":"studio","operation":"convoy_ping"}'
_KAT_SIGNATURE = (
    "1573190daa480ca417dd08e149808f3c5f97f1641cd2faed4b9752c78d61fd3f"
    "62abece043a6d51b871d6aee5f21f152cb9470acc9cc548bf3a595df2e4c460d")


def _kat_key():
    return ed25519.Ed25519PrivateKey.from_private_bytes(_KAT_SEED)


def test_known_answer_the_public_key_encoding_is_fixed():
    der = _kat_key().public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    assert der == _KAT_SPKI_DER


def test_known_answer_the_fingerprint_derivation_is_fixed():
    assert hk.fingerprint(_KAT_SPKI_DER) == _KAT_FINGERPRINT
    assert hk.format_fingerprint(_KAT_FINGERPRINT) == \
        "cvfp1-M188-6ZC5-0W2R-5K7Q-755G-K244-FK1H-5JW8"


def test_known_answer_the_envelope_signature_is_fixed():
    signer = hk.Ed25519Signer(private_key=_kat_key())
    assert signer.sign(_KAT_PAYLOAD) == _KAT_SIGNATURE
    assert signer.verify(_KAT_PAYLOAD, _KAT_SIGNATURE) is True
    # Cross-check the vector independently of this signer, so the test
    # cannot agree with a broken implementation about a broken value.
    _kat_key().public_key().verify(
        bytes.fromhex(_KAT_SIGNATURE),
        hk.ENVELOPE_DOMAIN_TAG + _KAT_PAYLOAD)


def test_known_answer_the_alg_tag_is_fixed():
    """The tag travels inside the signed subset and across the wire; a
    rename is a fleet-wide break, not a refactor."""
    assert hk.ALG_ED25519_V1 == "ed25519-v1"
    assert hk.Ed25519Signer(private_key=_kat_key()).alg == "ed25519-v1"
    assert hk.HostKeys.alg == "ed25519-v1"


def test_known_answer_the_identity_filenames_are_fixed():
    """The FILENAMES are a derivation too, and the highest-stakes one.

    _read_private_key returns None on FileNotFoundError, so renaming
    KEY_FILE makes every existing install's key invisible and the next
    start MINTS A FRESH IDENTITY -- exactly the catastrophe the whole
    refuse-to-start policy exists to prevent, arriving through a
    refactor rather than a bug. Every other test refers to hk.KEY_FILE
    symbolically and so follows a rename silently; this one does not.
    """
    assert hk.KEY_FILE == "identity.key"
    assert hk.CERT_FILE == "identity.cert.pem"


def test_known_answer_the_fingerprint_prefix_and_length_are_fixed():
    assert hk.FINGERPRINT_PREFIX == "cvfp1-"
    assert hk.FINGERPRINT_BYTES == 20
    assert hk.FINGERPRINT_GROUP == 4
    assert hk._CROCKFORD_ALPHABET == "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


# -- fingerprint ---------------------------------------------------------

def test_fingerprint_has_the_documented_shape(data_dir):
    keys = hk.load_or_create(data_dir)
    fp = keys.fingerprint
    assert fp.startswith("cvfp1-")
    body = fp[len("cvfp1-"):]
    groups = body.split("-")
    assert len(groups) == 8, "8 groups of 4, as the plan's example shows"
    assert all(len(g) == 4 for g in groups)
    assert body == body.lower(), "stored form is canonical lowercase"
    assert hk.is_valid_fingerprint(fp)
    # 20 bytes -> 160 bits -> exactly 32 symbols, no padding character.
    assert len(body.replace("-", "")) == 32
    assert "=" not in fp


def test_fingerprint_uses_crockford_and_drops_the_confusable_letters(data_dir):
    """i/l/o/u must never appear -- the format exists to be read aloud."""
    seen = set()
    for _ in range(60):
        key = ed25519.Ed25519PrivateKey.generate()
        der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        seen.update(hk.fingerprint(der)[len("cvfp1-"):].replace("-", ""))
    assert seen, "sanity: the sample produced symbols"
    assert not (seen & set("ilou"))
    assert seen <= set(hk._CROCKFORD_ALPHABET.lower())


def test_fingerprint_is_over_the_spki_not_the_certificate(data_dir):
    """A cert RE-ISSUE for the same key must not read as a new identity.

    This is the single most consequential line in the design: if the
    fingerprint moved when a certificate was regenerated, every peer
    would refuse this host with pin_mismatch after a routine repair.
    """
    keys = hk.load_or_create(data_dir)
    original_fp = keys.fingerprint
    original_cert = keys.certificate_pem

    os.unlink(keys.cert_path)
    reissued = hk.load_or_create(data_dir)

    assert reissued.origin == "cert_reissued"
    assert reissued.certificate_pem != original_cert, "a NEW certificate"
    assert reissued.fingerprint == original_fp, "the SAME identity"


def test_display_form_is_uppercase_and_never_stored(data_dir):
    keys = hk.load_or_create(data_dir)
    display = keys.fingerprint_display()
    assert display.startswith("cvfp1-")
    assert display[len("cvfp1-"):] == keys.fingerprint[len("cvfp1-"):].upper()
    assert display != keys.fingerprint
    # Round trip: the display form is presentation only.
    assert display.lower() == keys.fingerprint


@pytest.mark.parametrize("bad", [
    None, 42, "", "cvfp1-", "4T7K-9WQE-2M1X-BJ0R-HD5N-6PZA-C3VY-8SGF",
    "cvfp1-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy",           # 7 groups
    "cvfp1-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy-8sgf-0000",  # 9 groups
    "cvfp1-illo-9wqe-2m1x-bj0r-hd5n-6pza-c3vy-8sgf",      # out of alphabet
    "cvfp2-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy-8sgf",      # wrong prefix
])
def test_malformed_fingerprints_are_rejected(bad):
    assert hk.is_valid_fingerprint(bad) is False
    with pytest.raises(hk.HostKeyError):
        hk.format_fingerprint(bad)


# -- envelope round trip through the UNCHANGED protocol ------------------

def test_sign_verify_round_trip_through_the_unchanged_envelope(data_dir):
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(keys.signer())
    assert envelope["sig_alg"] == "ed25519-v1"
    # A verify-only signer built from the public key alone is what a
    # peer will hold -- verification must not need the private key.
    _verify(envelope, hk.verifier_from_public_der(keys.public_der))


def test_a_verify_only_signer_refuses_to_sign(data_dir):
    keys = hk.load_or_create(data_dir)
    verifier = hk.verifier_from_public_der(keys.public_der)
    with pytest.raises(hk.HostKeyError) as e:
        verifier.sign(b"anything")
    assert e.value.reason == "no_private_key"


def test_a_verifier_refuses_a_non_ed25519_public_key(data_dir):
    """Slice 3 builds verifiers from whatever DER a connecting peer's
    certificate carried. A P-256 key there is not "close enough" -- it
    is a different algorithm and a named refusal."""
    p256_der = ec.generate_private_key(ec.SECP256R1()).public_key(
        ).public_bytes(serialization.Encoding.DER,
                       serialization.PublicFormat.SubjectPublicKeyInfo)
    with pytest.raises(hk.HostKeyError) as e:
        hk.verifier_from_public_der(p256_der)
    assert e.value.reason == "unsupported_public_key"

    for junk in (b"", b"not der", None, 42, bytearray(b"\x30\x00")):
        with pytest.raises(hk.HostKeyError) as e:
            hk.verifier_from_public_der(junk)
        assert e.value.reason == "malformed_public_key"


def test_another_hosts_key_does_not_verify(data_dir, tmp_path):
    mine = hk.load_or_create(data_dir)
    theirs = hk.load_or_create(str(tmp_path / "other"))
    envelope = _envelope(mine.signer())
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, theirs.verifier())
    assert e.value.reason == "bad_signature"


@pytest.mark.parametrize("signature", [
    None, 42, b"\x00" * 64, "", "zz", "not-hex", "ab" * 63, "ab" * 65,
])
def test_malformed_signatures_are_false_never_an_exception(data_dir,
                                                           signature):
    """verify() returning False becomes a named `bad_signature`; a raw
    exception would escape as an unclassified 500 instead."""
    keys = hk.load_or_create(data_dir)
    assert keys.verifier().verify(b"payload", signature) is False


# -- cross-alg refusal, BOTH directions ----------------------------------

def test_an_ed25519_envelope_is_refused_by_an_hmac_verifier(data_dir):
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(keys.signer())
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, protocol.HmacSigner("shared-secret"))
    assert e.value.reason == "algorithm_mismatch"


def test_an_hmac_envelope_is_refused_by_an_ed25519_verifier(data_dir):
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(protocol.HmacSigner("shared-secret"))
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, keys.verifier())
    assert e.value.reason == "algorithm_mismatch"


def test_the_sig_alg_tag_cannot_be_relabelled_to_slip_through(data_dir):
    """S5, the PSK downgrade: relabelling an HMAC envelope as ed25519
    does not make an Ed25519 verifier accept it -- sig_alg is INSIDE the
    signed subset, so the relabel also destroys the signature."""
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(protocol.HmacSigner("shared-secret"))
    envelope["sig_alg"] = "ed25519-v1"
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, keys.verifier())
    assert e.value.reason == "bad_signature"


# -- tamper detection on EVERY signed field ------------------------------

# One tampered value per field in protocol._SIGNED_FIELDS, with the
# refusal each must produce. Three fields are checked BEFORE the
# signature (protocol, convoy_id, sig_alg) and so refuse earlier and
# more specifically; the other thirteen land on bad_signature. Nothing
# is allowed to verify.
_TAMPER = {
    "protocol": ("convoy/2", "protocol_mismatch"),
    "convoy_id": ("someone-elses-convoy", "namespace_mismatch"),
    "sig_alg": ("hmac-sha256", "algorithm_mismatch"),
    "request_id": ("00000000-0000-0000-0000-000000000000", "bad_signature"),
    "idempotency_key": ("replayed-key", "bad_signature"),
    "controller_id": ("ctl-attacker", "bad_signature"),
    "origin_host_id": ("a" * 32, "bad_signature"),
    "source_host_id": ("b" * 32, "bad_signature"),
    "target_node_id": ("c" * 32, "bad_signature"),
    "operation": ("execute_python", "bad_signature"),
    "arguments_sha256": ("f" * 64, "bad_signature"),
    "expected_runtime_id": ("rt_attacker", "bad_signature"),
    "created_unix": (12345.0, "bad_signature"),
    "budget_s": (99.0, "bad_signature"),
    "deadline_unix": (time.time() + 99999, "bad_signature"),
    "hop_limit": (99, "bad_signature"),
}


def test_the_tamper_table_covers_every_signed_field():
    """If a field is added to _SIGNED_FIELDS, this test fails until the
    tamper case for it is written -- coverage cannot silently rot."""
    assert set(_TAMPER) == set(protocol._SIGNED_FIELDS)


@pytest.mark.parametrize("field", sorted(_TAMPER))
def test_tampering_with_a_signed_field_is_refused(data_dir, field):
    keys = hk.load_or_create(data_dir)
    value, expected = _TAMPER[field]
    envelope = _envelope(keys.signer())
    assert envelope[field] != value, "the tamper must actually change it"
    envelope[field] = value
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, keys.verifier())
    assert e.value.reason == expected


def test_tampering_with_the_arguments_body_is_refused(data_dir):
    """The signature covers the DIGEST; the body is checked against it."""
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(keys.signer())
    envelope["arguments"] = {"a": 1, "and_also": "rm -rf"}
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, keys.verifier())
    assert e.value.reason == "arguments_tampered"


def test_tampering_with_the_signature_itself_is_refused(data_dir):
    keys = hk.load_or_create(data_dir)
    envelope = _envelope(keys.signer())
    flipped = bytearray(bytes.fromhex(envelope["signature"]))
    flipped[0] ^= 0x01
    envelope["signature"] = flipped.hex()
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, keys.verifier())
    assert e.value.reason == "bad_signature"


# -- domain separation ---------------------------------------------------

def _tls_certificate_verify_message():
    """The exact shape of TLS 1.3's CertificateVerify input (RFC 8446
    4.4.3): 64 octets of 0x20, a context string, a single 0x00, then the
    transcript hash. This is the OTHER thing this one key signs."""
    return (b"\x20" * 64
            + b"TLS 1.3, server CertificateVerify"
            + b"\x00"
            + hashlib.sha256(b"a handshake transcript").digest())


def test_a_tls_signature_is_not_accepted_as_an_envelope_signature(data_dir):
    """S8, the whole risk of sharing one key across TLS and envelopes.

    Signs a TLS-CertificateVerify-shaped message RAW, exactly as the TLS
    stack would with this same key, and asserts the envelope verifier
    refuses it -- because the verifier checks TAG||payload, and the TLS
    stack signed payload alone.
    """
    keys = hk.load_or_create(data_dir)
    with open(os.path.join(data_dir, hk.KEY_FILE), "rb") as f:
        raw_private = serialization.load_pem_private_key(f.read(),
                                                         password=None)
    tls_message = _tls_certificate_verify_message()
    tls_signature = raw_private.sign(tls_message).hex()

    assert keys.verifier().verify(tls_message, tls_signature) is False


def test_an_envelope_signature_is_not_a_valid_raw_signature(data_dir):
    """The other direction: what this signer produces is a signature
    over TAG||payload and is therefore worthless anywhere that verifies
    the payload directly (a TLS peer, for one)."""
    keys = hk.load_or_create(data_dir)
    with open(os.path.join(data_dir, hk.KEY_FILE), "rb") as f:
        raw_private = serialization.load_pem_private_key(f.read(),
                                                         password=None)
    tls_message = _tls_certificate_verify_message()
    envelope_signature = bytes.fromhex(keys.signer().sign(tls_message))

    with pytest.raises(InvalidSignature):
        raw_private.public_key().verify(envelope_signature, tls_message)
    # ... and it IS valid over the tagged bytes, so the test is proving
    # domain separation rather than a broken signer.
    raw_private.public_key().verify(envelope_signature,
                                    hk.ENVELOPE_DOMAIN_TAG + tls_message)


def test_the_domain_tag_is_applied_on_both_sides(data_dir):
    keys = hk.load_or_create(data_dir)
    payload = b'{"convoy_id":"studio"}'
    signature = keys.signer().sign(payload)
    assert keys.verifier().verify(payload, signature) is True
    # Pre-tagging the payload double-tags it and must NOT verify --
    # proof that verify() is not simply ignoring the tag.
    assert keys.verifier().verify(hk.ENVELOPE_DOMAIN_TAG + payload,
                                  signature) is False


def test_the_domain_tag_is_nul_terminated(data_dir):
    """A NUL terminator is what stops one tag's message space from being
    a prefix of another's when a second tag is added later."""
    assert hk.ENVELOPE_DOMAIN_TAG.endswith(b"\x00")
    assert hk.ENVELOPE_DOMAIN_TAG == b"convoy/1 envelope\x00"


# -- HmacSigner must be untouched ----------------------------------------

def test_hmac_signing_is_bit_for_bit_unchanged():
    """The PSK path must not have moved by a byte: HmacSigner is a plain
    HMAC-SHA256 over the canonical payload, with NO domain tag, exactly
    as it shipped. If this ever fails, every deployed PSK convoy breaks.
    """
    import hmac as _hmac
    signer = protocol.HmacSigner("shared-secret")
    payload = b'{"convoy_id":"studio"}'
    expected = _hmac.new(b"shared-secret", payload,
                         hashlib.sha256).hexdigest()
    assert signer.sign(payload) == expected


# -- storage: permissions, refusal, restart ------------------------------

def test_both_identity_files_are_written(data_dir):
    keys = hk.load_or_create(data_dir)
    assert keys.origin == "minted"
    assert os.path.isfile(os.path.join(data_dir, hk.KEY_FILE))
    assert os.path.isfile(os.path.join(data_dir, hk.CERT_FILE))
    assert "BEGIN PRIVATE KEY" in open(keys.key_path).read()
    assert "BEGIN CERTIFICATE" in keys.certificate_pem
    assert [n for n in os.listdir(data_dir) if n.endswith(".tmp")] == []


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits; NTFS ACLs govern on Windows")
def test_the_key_file_is_owner_only_on_posix(data_dir):
    keys = hk.load_or_create(data_dir)
    mode = os.stat(keys.key_path).st_mode
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH)), (
        "the host private key must not be group/world readable")
    assert not (mode & (stat.S_IWGRP | stat.S_IWOTH))
    assert stat.S_IMODE(mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits; NTFS ACLs govern on Windows")
def test_rotation_keeps_the_key_file_owner_only_on_posix(data_dir):
    keys = hk.load_or_create(data_dir)
    fresh = hk.rotate(data_dir, keys.fingerprint)
    assert stat.S_IMODE(os.stat(fresh.key_path).st_mode) == 0o600


# -- the private-key write, asserted WITHOUT POSIX mode bits ------------
#
# The two tests above are the only enforcement of 0600 / O_EXCL /
# O_NOFOLLOW / atomicity, and they SKIP on Windows -- so on the platform
# this is developed on, replacing _write_private with a plain
# open().write() (dropping all four properties at once) left the whole
# suite green. These two run everywhere.

def test_the_private_key_is_written_through_write_private(data_dir,
                                                          monkeypatch):
    """Platform-independent: the key material must pass through
    convoy_platform._write_private, which is where 0600, O_EXCL,
    O_NOFOLLOW and the atomic replace live."""
    seen = []
    real = cp._write_private

    def spy(path, data):
        seen.append((path, data))
        return real(path, data)

    monkeypatch.setattr(cp, "_write_private", spy)
    keys = hk.load_or_create(data_dir)

    key_writes = [(p, d) for p, d in seen if "PRIVATE KEY" in d]
    assert key_writes, "the private key did not go through _write_private"
    with open(keys.key_path, "r") as f:
        assert f.read() in [d for _, d in key_writes]


def test_the_private_key_write_cannot_bypass_write_private(data_dir,
                                                           monkeypatch):
    """The mutation guard: with _write_private disabled, NO key file may
    appear. A hand-rolled open().write() would leave one and fail here,
    on Windows as well as POSIX."""
    def refuse(path, data):
        raise OSError(13, "refused by the test")

    monkeypatch.setattr(cp, "_write_private", refuse)
    with pytest.raises(OSError):
        hk.load_or_create(data_dir)
    assert not os.path.exists(os.path.join(data_dir, hk.KEY_FILE)), (
        "a private key appeared without going through _write_private")


def test_loading_twice_returns_the_same_identity(data_dir):
    first = hk.load_or_create(data_dir)
    second = hk.load_or_create(data_dir)
    assert second.origin == "loaded"
    assert second.fingerprint == first.fingerprint
    assert second.public_der == first.public_der
    assert second.certificate_pem == first.certificate_pem


@pytest.mark.parametrize("content", [
    b"", b"not a pem at all", b"-----BEGIN PRIVATE KEY-----\ntruncated\n",
    b"\x00\x01\x02\x03",
])
def test_a_corrupt_key_file_refuses_to_start(data_dir, content):
    """The load must REFUSE, and -- the part that actually matters -- it
    must leave the damaged file exactly as it found it. A fresh mint on
    top of a damaged key orphans every peer relationship on every peer."""
    path = os.path.join(data_dir, hk.KEY_FILE)
    with open(path, "wb") as f:
        f.write(content)

    with pytest.raises(hk.HostKeyError) as e:
        hk.load_or_create(data_dir)
    assert e.value.reason == "identity_unreadable"
    assert "Refusing" in e.value.detail

    with open(path, "rb") as f:
        assert f.read() == content, "the damaged key was NOT overwritten"
    assert not os.path.exists(os.path.join(data_dir, hk.CERT_FILE))


def test_an_unreadable_key_file_refuses_to_start(data_dir):
    """Not only a PARSE failure: any OSError that is not
    FileNotFoundError is a refusal, because the key may still exist and
    be perfectly good. A directory in the key's place raises
    IsADirectoryError on POSIX and PermissionError on Windows -- both
    OSError, neither FileNotFoundError."""
    os.makedirs(os.path.join(data_dir, hk.KEY_FILE))
    with pytest.raises(hk.HostKeyError) as e:
        hk.load_or_create(data_dir)
    assert e.value.reason == "identity_unreadable"


def test_a_key_of_the_wrong_algorithm_refuses_to_start(data_dir):
    """A P-256 key where Ed25519 belongs is somebody's identity too --
    replace it and whatever it was is gone."""
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    with open(os.path.join(data_dir, hk.KEY_FILE), "wb") as f:
        f.write(pem)
    with pytest.raises(hk.HostKeyError) as e:
        hk.load_or_create(data_dir)
    assert e.value.reason == "identity_wrong_algorithm"


def test_an_encrypted_key_refuses_rather_than_re_minting(data_dir):
    pem = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"))
    path = os.path.join(data_dir, hk.KEY_FILE)
    with open(path, "wb") as f:
        f.write(pem)
    with pytest.raises(hk.HostKeyError) as e:
        hk.load_or_create(data_dir)
    assert e.value.reason == "identity_unreadable"
    with open(path, "rb") as f:
        assert f.read() == pem


def test_a_damaged_certificate_is_re_issued_not_refused(data_dir):
    """The cert is a DERIVED artifact: refusing on a damaged one would
    hand a denial of service to anyone able to write a single file."""
    keys = hk.load_or_create(data_dir)
    with open(keys.cert_path, "w") as f:
        f.write("this is not a certificate\n")
    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued"
    assert healed.fingerprint == keys.fingerprint
    assert "BEGIN CERTIFICATE" in open(healed.cert_path).read()


def test_a_certificate_for_somebody_elses_key_is_replaced(data_dir):
    """A well-formed cert that does not match the private key is not
    this host's certificate, however convincing it looks."""
    keys = hk.load_or_create(data_dir)
    stranger = hk.load_or_create(os.path.join(data_dir, "stranger"))
    with open(keys.cert_path, "w") as f:
        f.write(stranger.certificate_pem)

    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued"
    assert healed.fingerprint == keys.fingerprint
    assert healed.certificate_pem != stranger.certificate_pem


def test_a_missing_certificate_is_re_issued_from_the_key(data_dir):
    keys = hk.load_or_create(data_dir)
    os.unlink(keys.cert_path)
    healed = hk.load_or_create(data_dir)
    assert healed.fingerprint == keys.fingerprint
    assert os.path.isfile(healed.cert_path)


# -- rotation ------------------------------------------------------------

def test_rotation_changes_the_identity(data_dir):
    before = hk.load_or_create(data_dir)
    after = hk.rotate(data_dir, before.fingerprint)
    assert after.origin == "rotated"
    assert after.fingerprint != before.fingerprint
    assert after.public_der != before.public_der
    assert after.certificate_pem != before.certificate_pem
    # And the change is DURABLE: a reload sees the new identity, so
    # every peer's pin is now stale until a human re-admits.
    assert hk.load_or_create(data_dir).fingerprint == after.fingerprint


def test_an_envelope_signed_before_rotation_no_longer_verifies(data_dir):
    before = hk.load_or_create(data_dir)
    envelope = _envelope(before.signer())
    after = hk.rotate(data_dir, before.fingerprint)
    with pytest.raises(protocol.EnvelopeRejected) as e:
        _verify(envelope, after.verifier())
    assert e.value.reason == "bad_signature"


# -- rotation is COMPARE-AND-SWAP --------------------------------------

@pytest.mark.parametrize("confirm", [
    None, "", 0, {}, [], 42, True,
    "not-a-fingerprint",
    "cvfp1-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy",     # 7 groups
])
def test_rotation_without_a_valid_confirmation_is_refused(data_dir, confirm):
    """A bare command destroys the fleet identity. An echo of the
    CURRENT fingerprint makes a blind or replayed call harmless."""
    before = hk.load_or_create(data_dir)
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, confirm)
    assert e.value.reason == "rotation_unconfirmed"
    assert hk.load_or_create(data_dir).fingerprint == before.fingerprint


def test_rotation_confirming_the_wrong_fingerprint_is_refused(data_dir,
                                                              tmp_path):
    """Refusing to rotate an identity the caller was not looking at --
    the same shape as A-22's expected_runtime_id."""
    before = hk.load_or_create(data_dir)
    stranger = hk.load_or_create(str(tmp_path / "stranger"))
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, stranger.fingerprint)
    assert e.value.reason == "rotation_fingerprint_mismatch"
    assert before.fingerprint in e.value.detail
    assert hk.load_or_create(data_dir).fingerprint == before.fingerprint


def test_rotation_compares_against_disk_not_the_callers_memory(data_dir):
    """A stale in-memory fingerprint must NOT be able to confirm.

    Two rotations racing: the second caller still holds the pre-first
    fingerprint. Comparing against its own memory would let it rotate;
    comparing against DISK refuses.
    """
    first = hk.load_or_create(data_dir)
    second = hk.rotate(data_dir, first.fingerprint)
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, first.fingerprint)          # stale echo
    assert e.value.reason == "rotation_fingerprint_mismatch"
    assert hk.load_or_create(data_dir).fingerprint == second.fingerprint


def test_a_refused_rotation_never_touches_the_disk(data_dir):
    """The refusal must be TRUE: byte-identical files afterwards."""
    keys = hk.load_or_create(data_dir)
    before = {name: open(os.path.join(data_dir, name), "rb").read()
              for name in (hk.KEY_FILE, hk.CERT_FILE)}
    with pytest.raises(hk.RotationRefused):
        hk.rotate(data_dir, None)
    after = {name: open(os.path.join(data_dir, name), "rb").read()
             for name in (hk.KEY_FILE, hk.CERT_FILE)}
    assert after == before
    assert hk.load_or_create(data_dir).fingerprint == keys.fingerprint


# -- rotation is ALL OR NOTHING ----------------------------------------

class _FailingWrite:
    """Let _write_private through until a target path is written, then
    raise the OSError a Windows sharing violation / ENOSPC / AV lock
    really produces -- which convoy_platform._write_private re-raises
    after its 8 retries, and which used to escape rotate() entirely."""

    def __init__(self, real, target_name, error=None):
        self.real = real
        self.target_name = target_name
        self.error = error or PermissionError(13, "locked by the test")
        self.failures = 0

    def __call__(self, path, data):
        if os.path.basename(path) == self.target_name:
            self.failures += 1
            raise self.error
        return self.real(path, data)


def test_a_failed_certificate_commit_rolls_the_key_back(data_dir,
                                                        monkeypatch):
    """THE BLOCKER. The key was written, the cert write failed, and the
    OLD key must be back on disk -- otherwise the next boot silently
    comes up as an identity nobody was ever told about, orphaning every
    peer pin, while the caller was told the rotation was refused."""
    keys = hk.load_or_create(data_dir)
    original_key = open(keys.key_path, "rb").read()
    original_cert = open(keys.cert_path, "rb").read()

    failing = _FailingWrite(cp._write_private, hk.CERT_FILE)
    monkeypatch.setattr(cp, "_write_private", failing)
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, keys.fingerprint)
    monkeypatch.undo()

    assert e.value.reason == "rotation_failed"
    assert "rolled back" in e.value.detail
    assert open(keys.key_path, "rb").read() == original_key
    assert open(keys.cert_path, "rb").read() == original_cert
    # The claim the refusal makes must be true on the NEXT BOOT too.
    assert hk.load_or_create(data_dir).fingerprint == keys.fingerprint


def test_a_failed_key_commit_leaves_the_identity_untouched(data_dir,
                                                           monkeypatch):
    keys = hk.load_or_create(data_dir)
    original = open(keys.key_path, "rb").read()

    failing = _FailingWrite(cp._write_private, hk.KEY_FILE)
    monkeypatch.setattr(cp, "_write_private", failing)
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, keys.fingerprint)
    monkeypatch.undo()

    assert e.value.reason == "rotation_failed"
    assert open(keys.key_path, "rb").read() == original
    assert hk.load_or_create(data_dir).fingerprint == keys.fingerprint


def test_an_unrollbackable_failure_is_indeterminate_never_a_refusal(
        data_dir, monkeypatch):
    """The ONE case where the disk really did move. It must NOT be
    reported as a refusal -- a false refusal is what made the original
    bug invisible."""
    keys = hk.load_or_create(data_dir)
    real = cp._write_private
    state = {"cert_seen": False}

    def hostile(path, data):
        name = os.path.basename(path)
        if name == hk.CERT_FILE:
            state["cert_seen"] = True
            raise PermissionError(13, "cert locked")
        if name == hk.KEY_FILE and state["cert_seen"]:
            raise PermissionError(13, "key locked too -- rollback fails")
        return real(path, data)

    monkeypatch.setattr(cp, "_write_private", hostile)
    with pytest.raises(hk.RotationIndeterminate) as e:
        hk.rotate(data_dir, keys.fingerprint)
    monkeypatch.undo()

    assert e.value.reason == "rotation_indeterminate"
    assert "CHANGED" in e.value.detail
    # RotationIndeterminate must NOT be catchable as a plain refusal.
    assert not isinstance(e.value, hk.RotationRefused)
    assert isinstance(e.value, hk.HostKeyError)


def test_staging_failure_refuses_before_anything_live_is_touched(
        data_dir, monkeypatch):
    keys = hk.load_or_create(data_dir)
    original = open(keys.key_path, "rb").read()
    real = cp._write_private

    def stage_fails(path, data):
        if path.endswith(".rotate"):
            raise OSError(28, "no space left on device")
        return real(path, data)

    monkeypatch.setattr(cp, "_write_private", stage_fails)
    with pytest.raises(hk.RotationRefused) as e:
        hk.rotate(data_dir, keys.fingerprint)
    monkeypatch.undo()

    assert e.value.reason == "rotation_failed"
    assert "UNCHANGED" in e.value.detail
    assert open(keys.key_path, "rb").read() == original


def test_rotation_leaves_no_stage_files_behind(data_dir):
    keys = hk.load_or_create(data_dir)
    hk.rotate(data_dir, keys.fingerprint)
    leftovers = [n for n in os.listdir(data_dir)
                 if ".rotate" in n or ".claim" in n or n.endswith(".tmp")]
    assert leftovers == []


# -- the mint CLAIMS the destination -----------------------------------

def test_a_concurrent_mint_converges_on_the_key_that_landed(data_dir):
    """Read-then-write with an unconditional replace loses an identity
    roughly every time two processes start together (the panel measured
    11 of 12), leaving the loser signing envelopes with a key no peer
    will ever see. Every racer must end up on the key that is ON DISK.
    """
    results = []
    errors = []
    start = threading.Barrier(6)

    def racer():
        try:
            start.wait(timeout=30)
            results.append(hk.load_or_create(data_dir))
        except Exception as e:                  # recorded, not swallowed
            errors.append(e)

    threads = [threading.Thread(target=racer) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    assert errors == [], errors
    assert len(results) == 6
    on_disk = hk.load_or_create(data_dir).fingerprint
    assert {k.fingerprint for k in results} == {on_disk}, (
        "a racer is running an identity that is not on disk")
    assert sum(1 for k in results if k.origin == "minted") <= 1


def test_the_mint_never_overwrites_an_existing_key(data_dir):
    """_claim_file's create-if-absent, asserted directly: the mint path
    must be structurally incapable of replacing a live identity."""
    keys = hk.load_or_create(data_dir)
    original = open(keys.key_path, "rb").read()
    landed, created = hk._mint_private_key(keys.key_path)
    assert created is False, "a second mint claimed an occupied path"
    assert open(keys.key_path, "rb").read() == original
    assert hk.fingerprint(hk._public_der(landed.public_key())) == \
        keys.fingerprint


def test_an_adopted_key_is_reported_as_adopted(data_dir):
    """The loser of a mint race is a distinct, auditable origin -- it is
    the case that used to silently produce a phantom identity."""
    first = hk.load_or_create(data_dir)
    landed, created = hk._mint_private_key(first.key_path)
    assert created is False
    assert landed is not None


def test_the_certificate_carries_the_key_it_claims(data_dir):
    """Whatever the cert says in its subject, what matters is that its
    public key is this key -- that is what the pin commits to."""
    keys = hk.load_or_create(data_dir)
    certificate = x509.load_pem_x509_certificate(
        keys.certificate_pem.encode("ascii"))
    der = certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)
    assert der == keys.public_der
    assert hk.fingerprint(der) == keys.fingerprint
    assert certificate.issuer == certificate.subject
    # SELF-SIGNED, not merely self-issued.
    certificate.public_key().verify(certificate.signature,
                                    certificate.tbs_certificate_bytes)


# -- the certificate PROFILE is load-bearing and pinned ----------------

def _issued(data_dir, now=None):
    keys = hk.load_or_create(data_dir, now=now)
    return keys, x509.load_pem_x509_certificate(
        keys.certificate_pem.encode("ascii"))


def test_the_certificate_is_not_a_ca(data_dir):
    """THE one with real security weight. Slice 3 pins peers with
    load_verify_locations(cadata=<peer PEM>), which makes each pinned
    LEAF an OpenSSL trust anchor -- with ca=True that peer can issue
    certificates for any other identity and have them accepted at the
    TLS layer (the review panel built the chain and it handshook).
    The app-layer SPKI recomputation is the second defense; this is the
    first, and neither was tested before."""
    _, certificate = _issued(data_dir)
    basic = certificate.extensions.get_extension_for_class(
        x509.BasicConstraints)
    assert basic.critical is True
    assert basic.value.ca is False
    assert basic.value.path_length is None
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.value.key_cert_sign is False
    assert usage.value.crl_sign is False


def test_the_certificate_key_usage_is_exactly_digital_signature(data_dir):
    _, certificate = _issued(data_dir)
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage)
    assert usage.critical is True
    value = usage.value
    assert value.digital_signature is True
    for bit in ("content_commitment", "key_encipherment",
                "data_encipherment", "key_agreement", "key_cert_sign",
                "crl_sign"):
        assert getattr(value, bit) is False, bit


def test_the_certificate_carries_both_mutual_tls_roles(data_dir):
    """One certificate serves this host as SERVER to inbound peers and
    as CLIENT to outbound ones. Dropping either role silently breaks one
    direction of the peer leg -- and the spike test cannot catch it,
    because it loads one context for both ends."""
    _, certificate = _issued(data_dir)
    extended = certificate.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage).value
    assert set(extended) == {x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                             x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH}


def test_the_certificate_names_its_own_key_and_cannot_lie(data_dir):
    """CN and SAN are decorative and nothing reads them -- but the whole
    point of the cvfp1- format is that two operators read it aloud, so a
    certificate that self-reports a fingerprint which is not its key's
    is an operator-deception surface the moment anyone dumps it."""
    keys, certificate = _issued(data_dir)
    common = certificate.subject.get_attributes_for_oid(
        x509.oid.NameOID.COMMON_NAME)[0].value
    assert common == keys.fingerprint
    names = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    assert names == [keys.fingerprint]
    ski = certificate.extensions.get_extension_for_class(
        x509.SubjectKeyIdentifier).value
    assert ski == x509.SubjectKeyIdentifier.from_public_key(
        certificate.public_key())


def test_the_certificate_is_backdated_for_a_peer_whose_clock_is_behind(
        data_dir):
    """CERT_BACKDATE_DAYS is the documented mitigation for a peer whose
    clock is a few hours behind seeing a fresh cert as not-yet-valid.
    With no assertion it could be simplified to 0 and the failure would
    surface only in slice 3 as an unexplained handshake error."""
    now = datetime.datetime(2030, 3, 4, 5, 6, 7)
    _, certificate = _issued(data_dir, now=now)
    not_before, _ = hk._certificate_validity(certificate)
    assert not_before < now
    assert (now - not_before) == datetime.timedelta(
        days=hk.CERT_BACKDATE_DAYS)
    assert hk.CERT_BACKDATE_DAYS >= 1


@pytest.mark.parametrize("mint_year,expect_clamped", [
    (2026, False),      # 20 years lands in 2046, inside UTCTime
    (2029, False),      # 2049 -- the last year that still fits
    (2031, True),       # would be 2051; must clamp
    (2045, True),
])
def test_the_certificate_never_crosses_the_utctime_horizon(
        data_dir, mint_year, expect_clamped):
    """RFC 5280 4.1.2.5 requires dates from 2050 on to be
    GeneralizedTime. A nominal 20-year validity crosses that line for
    real in August 2030, so the constant alone was a promise that
    expires -- and the only guard was `assert year < 2050`, which would
    have started failing in CI as a mystery. The clamp makes it true at
    every mint date."""
    now = datetime.datetime(mint_year, 8, 1)
    _, certificate = _issued(data_dir, now=now)
    _, not_after = hk._certificate_validity(certificate)
    assert not_after <= hk.UTCTIME_HORIZON
    assert not_after.year < 2050
    if expect_clamped:
        assert not_after == hk.UTCTIME_HORIZON
    else:
        assert not_after == now + datetime.timedelta(
            days=hk.CERT_VALIDITY_DAYS)
        assert not_after.year > mint_year + 5


# -- an unusable certificate is REPAIRED, not kept ---------------------

def _plant_certificate(path, private_key, **over):
    """Write a certificate for this key with a deliberately wrong
    field, exactly as a bad backup or a wrong clock would leave one."""
    now = over.pop("now", hk._utcnow())
    public_key = private_key.public_key()
    fp = hk.fingerprint(hk._public_der(public_key))
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, fp)])
    builder = (x509.CertificateBuilder()
               .subject_name(over.pop("subject", name))
               .issuer_name(over.pop("issuer", name))
               .public_key(over.pop("public_key", public_key))
               .serial_number(x509.random_serial_number())
               .not_valid_before(over.pop("not_valid_before",
                                          now - datetime.timedelta(days=1)))
               .not_valid_after(over.pop("not_valid_after",
                                         now + datetime.timedelta(days=365))))
    for extension, critical in over.pop("extensions", [
            (x509.BasicConstraints(ca=False, path_length=None), True),
            (x509.KeyUsage(digital_signature=True, content_commitment=False,
                           key_encipherment=False, data_encipherment=False,
                           key_agreement=False, key_cert_sign=False,
                           crl_sign=False, encipher_only=False,
                           decipher_only=False), True),
            (x509.ExtendedKeyUsage([
                x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), False),
            (x509.SubjectAlternativeName([x509.DNSName(fp)]), False),
            (x509.SubjectKeyIdentifier.from_public_key(public_key), False)]):
        builder = builder.add_extension(extension, critical=critical)
    certificate = builder.sign(over.pop("signer", private_key), None)
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    with open(path, "wb") as f:
        f.write(pem)
    return pem


def _load_key(data_dir):
    with open(os.path.join(data_dir, hk.KEY_FILE), "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def test_an_expired_certificate_is_re_issued(data_dir):
    """PROVEN BY THE PANEL as kept-forever: SPKI match alone was the
    whole check, so an expired cert stayed on disk and then failed the
    TLS handshake on BOTH legs with no self-heal -- on a path designed
    to be self-healing. Reachable without an attacker: a data dir
    restored from backup, or a cert minted while the machine's clock was
    wrong, which is exactly the show-machine condition the plan spends a
    paragraph refusing at admission."""
    keys = hk.load_or_create(data_dir)
    stale = datetime.datetime(2020, 1, 1)
    planted = _plant_certificate(
        keys.cert_path, _load_key(data_dir),
        now=stale,
        not_valid_before=stale - datetime.timedelta(days=1),
        not_valid_after=stale + datetime.timedelta(days=30))

    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued"
    assert healed.fingerprint == keys.fingerprint, "the identity is stable"
    assert open(healed.cert_path, "rb").read() != planted
    _, not_after = hk._certificate_validity(x509.load_pem_x509_certificate(
        healed.certificate_pem.encode("ascii")))
    assert not_after > hk._utcnow()


def test_a_not_yet_valid_certificate_is_re_issued(data_dir):
    """The wrong-clock case in the other direction: a cert minted while
    the RTC was in the future was never re-checked either."""
    keys = hk.load_or_create(data_dir)
    future = hk._utcnow() + datetime.timedelta(days=3650)
    _plant_certificate(keys.cert_path, _load_key(data_dir),
                       not_valid_before=future,
                       not_valid_after=future + datetime.timedelta(days=365))
    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued"
    assert healed.fingerprint == keys.fingerprint


def test_a_stranger_signed_certificate_is_re_issued(data_dir, tmp_path):
    """Right key inside, wrong signature over it. Self-ISSUED (matching
    subject and issuer) proves nothing; the self-SIGNATURE is the check."""
    keys = hk.load_or_create(data_dir)
    stranger = ed25519.Ed25519PrivateKey.generate()
    _plant_certificate(keys.cert_path, _load_key(data_dir),
                       signer=stranger)
    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued"
    assert healed.fingerprint == keys.fingerprint


def _profile_extensions(public_key, fp, flaw=None):
    """The shipped extension set with EXACTLY ONE flaw introduced.

    One flaw at a time is the whole point. The first version of this
    helper dropped SAN and SKI from every case as well, so a
    CA-flagged certificate was rejected for being profile-INCOMPLETE
    and the CA check itself was never reached -- disabling it left the
    suite green (found by mutation, mutant `no_profile_check`). Each
    case below is otherwise perfect, so each isolates one check.
    """
    ca = flaw == "ca_flagged"
    roles = [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]
    if flaw != "server_auth_only":
        roles.append(x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH)
    extensions = [
        (x509.BasicConstraints(ca=ca, path_length=None), True),
        (x509.KeyUsage(digital_signature=flaw != "no_digital_signature",
                       content_commitment=False, key_encipherment=False,
                       data_encipherment=False, key_agreement=False,
                       key_cert_sign=ca, crl_sign=ca,
                       encipher_only=False, decipher_only=False), True),
        (x509.ExtendedKeyUsage(roles), False),
    ]
    if flaw != "no_san":
        extensions.append(
            (x509.SubjectAlternativeName([x509.DNSName(fp)]), False))
    if flaw != "no_ski":
        extensions.append(
            (x509.SubjectKeyIdentifier.from_public_key(public_key), False))
    return extensions


@pytest.mark.parametrize("flaw", [
    "ca_flagged", "server_auth_only", "no_digital_signature",
    "no_san", "no_ski", "everything_stripped",
])
def test_a_certificate_with_the_wrong_profile_is_re_issued(data_dir, flaw):
    """A profile-stripped or CA-flagged cert carrying the right key was
    accepted and kept forever. It is the artifact slice 3 pins as an
    OpenSSL TRUST ANCHOR, so it has to be repaired like any other
    damage. ONE flaw per case, so each check is isolated."""
    keys = hk.load_or_create(data_dir)
    private_key = _load_key(data_dir)
    fp = keys.fingerprint
    extensions = ([] if flaw == "everything_stripped"
                  else _profile_extensions(private_key.public_key(), fp,
                                           flaw))
    _plant_certificate(keys.cert_path, private_key, extensions=extensions)

    healed = hk.load_or_create(data_dir)
    assert healed.origin == "cert_reissued", flaw
    assert healed.fingerprint == keys.fingerprint
    repaired = x509.load_pem_x509_certificate(
        healed.certificate_pem.encode("ascii"))
    assert repaired.extensions.get_extension_for_class(
        x509.BasicConstraints).value.ca is False


def test_the_wrong_profile_cases_are_otherwise_perfect(data_dir):
    """Guards the guard: with NO flaw, _profile_extensions must produce
    a certificate the repair leaves alone. Otherwise every case above
    could be passing for a reason that has nothing to do with its
    flaw -- which is exactly how the CA check went untested."""
    keys = hk.load_or_create(data_dir)
    private_key = _load_key(data_dir)
    planted = _plant_certificate(
        keys.cert_path, private_key,
        extensions=_profile_extensions(private_key.public_key(),
                                       keys.fingerprint, flaw=None))
    again = hk.load_or_create(data_dir)
    assert again.origin == "loaded", "a flawless planted cert was replaced"
    assert open(keys.cert_path, "rb").read() == planted


def test_a_healthy_certificate_is_left_alone(data_dir):
    """The repair must not be a rewrite-on-every-boot: an intact cert is
    kept byte for byte, or the 'derived artifact' policy becomes churn."""
    keys = hk.load_or_create(data_dir)
    stored = open(keys.cert_path, "rb").read()
    again = hk.load_or_create(data_dir)
    assert again.origin == "loaded"
    assert open(keys.cert_path, "rb").read() == stored
    assert again.certificate_pem == keys.certificate_pem


# -- an UNWRITABLE certificate degrades; it does not kill the daemon ---

def test_an_unwritable_certificate_degrades_rather_than_refusing(
        data_dir, monkeypatch):
    """THE BLOCKER. Repair covered a CORRUPT cert but not an UNWRITABLE
    one, so one local write to identity.cert.pem raised a raw
    PermissionError out of HostApp.__init__ and the daemon never bound
    -- a crash-restart loop under the A-36 supervisor, and precisely the
    denial of service the repair policy exists to avoid. The KEY is
    identity and still refuses; the CERT is derived and must not."""
    keys = hk.load_or_create(data_dir)
    os.unlink(keys.cert_path)

    failing = _FailingWrite(cp._write_private, hk.CERT_FILE)
    monkeypatch.setattr(cp, "_write_private", failing)
    degraded = hk.load_or_create(data_dir)
    monkeypatch.undo()

    assert failing.failures >= 1, "the test did not exercise the write"
    assert degraded.fingerprint == keys.fingerprint, "identity intact"
    assert degraded.certificate_pem is None
    assert degraded.cert_reason.startswith("cert_unwritable")
    # The identity is fully usable for ENVELOPES -- only the TLS
    # artifact is missing, which is slice 3's problem to refuse on.
    envelope = _envelope(degraded.signer())
    _verify(envelope, degraded.verifier())


def test_an_unwritable_certificate_still_lets_the_host_app_start(
        tmp_path, monkeypatch):
    directory = str(tmp_path / "state")
    os.makedirs(directory)
    hk.load_or_create(directory)
    os.unlink(os.path.join(directory, hk.CERT_FILE))

    monkeypatch.setattr(cp, "_write_private",
                        _FailingWrite(cp._write_private, hk.CERT_FILE))
    server = Server(directory)
    monkeypatch.undo()
    try:
        code, body = server.call("/identity")
        assert code == 200 and body["ok"] is True
        assert body["certificate"] is False
        assert body["certificate_reason"].startswith("cert_unwritable")

        code, body = server.call("/status")
        assert code == 200
        assert body["identity_reason"] is None, "the identity is FINE"
        assert body["identity_certificate"] is False
        assert body["identity_cert_reason"].startswith("cert_unwritable")
    finally:
        server.stop()


# -- THE SPIKE, kept alive as a test -------------------------------------

@pytest.mark.skipif(not hasattr(__import__("ssl"), "TLSVersion"),
                    reason="no TLS version control in this Python")
def test_spike_the_minted_cert_negotiates_mutual_tls13_on_loopback(data_dir):
    """The 20-minute spike the plan required, pinned so the CI matrix
    re-proves it on windows-latest AND macos-latest every run.

    Slice 1 ships NO listener -- this binds 127.0.0.1:0 inside the test
    only, to answer the one question the "one key or two" decision rests
    on: can CPython's BUNDLED OpenSSL negotiate TLS 1.3 with an Ed25519
    LEAF certificate? If a platform ever answers no, the fallback is a
    P-256 cert with Ed25519 kept for envelopes -- and this test is where
    that would first show up, before slice 3 is built on the assumption.
    """
    import socket
    import ssl

    keys = hk.load_or_create(data_dir)
    pem = keys.certificate_pem
    out = {}

    def serve():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(keys.cert_path, keys.key_path)
        ctx.verify_mode = ssl.CERT_REQUIRED      # MUTUAL
        ctx.load_verify_locations(cadata=pem)    # pinned self-signed
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        out["port"] = srv.getsockname()[1]
        ready.set()
        try:
            raw, _ = srv.accept()
            with ctx.wrap_socket(raw, server_side=True) as tls:
                out["server_version"] = tls.version()
                out["peer_der"] = tls.getpeercert(binary_form=True)
                out["received"] = tls.recv(64)
                # The ACK is what makes this deterministic: the client
                # blocks on it, so the client cannot close before this
                # recv has completed. Without it the client's close
                # races the server's read and the server sees a reset
                # (WinError 10053) on a handshake that in fact
                # succeeded -- a flake that would read as a spike
                # FAILURE.
                tls.sendall(b"ack")
        except Exception as e:              # surfaced by the assertions
            out["server_error"] = repr(e)
        finally:
            srv.close()

    ready = threading.Event()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(20)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False               # pinning, not PKI names
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=pem)
    ctx.load_cert_chain(keys.cert_path, keys.key_path)

    with socket.create_connection(("127.0.0.1", out["port"]),
                                  timeout=20) as raw:
        with ctx.wrap_socket(raw) as tls:
            client_version = tls.version()
            server_der = tls.getpeercert(binary_form=True)
            tls.sendall(b"hello")
            acknowledged = tls.recv(64)
    thread.join(20)

    assert "server_error" not in out, out.get("server_error")
    assert client_version == "TLSv1.3"
    assert out["server_version"] == "TLSv1.3"
    assert out["received"] == b"hello"
    assert acknowledged == b"ack"
    # The post-handshake move slice 3 makes: recompute the fingerprint
    # LOCALLY from the DER the peer actually presented.
    from cryptography import x509
    for der in (server_der, out["peer_der"]):
        presented = x509.load_der_x509_certificate(der)
        spki = presented.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        assert hk.fingerprint(spki) == keys.fingerprint


# -- the routes ----------------------------------------------------------

def test_identity_route_returns_the_public_identity(server):
    code, body = server.call("/identity")
    assert code == 200 and body["ok"] is True
    assert body["host_id"] == server.app.host_id
    assert body["alg"] == "ed25519-v1"
    assert hk.is_valid_fingerprint(body["fingerprint"])
    assert body["fingerprint_display"] == hk.format_fingerprint(
        body["fingerprint"])


def test_identity_route_never_leaks_the_private_key(server):
    """Not a shape check -- a SUBSTRING hunt over the whole response for
    the actual private-key bytes on disk, plus every PRIVATE-key PEM
    header that could carry them.

    The route now DOES carry the public certificate PEM (slice 3: a peer
    must obtain it to pin this host), so '-----BEGIN' is no longer a
    leak marker -- 'BEGIN CERTIFICATE' is expected and safe. The hunt
    narrows to PRIVATE-key markers, which the certificate can never
    contain, and the actual secret bytes of the key file."""
    code, body = server.call("/identity")
    assert code == 200
    blob = json.dumps(body)
    with open(server.app.hostkeys.key_path, "r") as f:
        stored = f.read()
    secret_body = "".join(stored.splitlines()[1:-1])
    assert secret_body not in blob
    for marker in ("PRIVATE KEY", "BEGIN EC PRIVATE", "BEGIN RSA PRIVATE",
                   "BEGIN OPENSSH PRIVATE"):
        assert marker not in blob
    # The PUBLIC certificate IS published here now, deliberately: it is
    # the material an operator copies to a peer's /peers/admit to pin
    # this host. Lock that in so a future refactor cannot quietly drop it.
    assert body.get("certificate_pem")
    assert "BEGIN CERTIFICATE" in body["certificate_pem"]
    assert "PRIVATE KEY" not in body["certificate_pem"]


def test_status_never_leaks_the_private_key(server):
    code, body = server.call("/status")
    assert code == 200
    with open(server.app.hostkeys.key_path, "r") as f:
        secret_body = "".join(f.read().splitlines()[1:-1])
    assert secret_body not in json.dumps(body)


def test_status_reports_the_identity(server):
    code, body = server.call("/status")
    assert code == 200
    assert body["identity_alg"] == "ed25519-v1"
    assert body["identity_fingerprint"] == server.app.hostkeys.fingerprint
    assert body["identity_reason"] is None
    assert body["identity_certificate"] is True
    assert body["identity_cert_reason"] is None


@pytest.mark.parametrize("path,method,payload", [
    ("/identity", None, None),
    ("/identity/rotate", None, {}),
])
def test_the_identity_routes_require_the_token(server, path, method, payload):
    code, body = server.call(path, payload, token=None, method=method)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_rotate_route_mints_a_new_identity(server):
    _, before = server.call("/identity")
    code, body = server.call(
        "/identity/rotate",
        {"confirm_fingerprint": before["fingerprint"]})
    assert code == 200 and body["ok"] is True
    assert body["previous_fingerprint"] == before["fingerprint"]
    assert body["fingerprint"] != before["fingerprint"]
    assert hk.is_valid_fingerprint(body["fingerprint"])

    _, after = server.call("/identity")
    assert after["fingerprint"] == body["fingerprint"]
    assert server.app.hostkeys.fingerprint == body["fingerprint"]
    # host_id is NOT the fingerprint and does not move with it: the pin
    # a peer records is the BINDING of the two.
    assert after["host_id"] == before["host_id"]


def test_rotate_route_accepts_the_display_form_an_operator_copies(server):
    """The uppercase grouped form is what is on the screen. Refusing a
    correct answer over letter case would train people to paste blind."""
    _, before = server.call("/identity")
    code, body = server.call(
        "/identity/rotate",
        {"confirm_fingerprint": before["fingerprint_display"]})
    assert code == 200, body
    assert body["previous_fingerprint"] == before["fingerprint"]


@pytest.mark.parametrize("payload,reason", [
    ({}, "rotation_unconfirmed"),
    ({"confirm_fingerprint": ""}, "rotation_unconfirmed"),
    ({"confirm_fingerprint": None}, "rotation_unconfirmed"),
    ({"confirm_fingerprint": 42}, "rotation_unconfirmed"),
    ({"confirm_fingerprint": "cvfp1-not-a-real-fingerprint"},
     "rotation_unconfirmed"),
    ({"confirm_fingerprint":
      "cvfp1-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy-8sgf"},
     "rotation_fingerprint_mismatch"),
])
def test_rotate_route_refuses_without_the_right_confirmation(server, payload,
                                                             reason):
    """THE BLOCKER: `{}` used to be sufficient, and the suite's own test
    posted exactly that. Destroying the fleet identity now requires
    naming the identity being destroyed."""
    _, before = server.call("/identity")
    code, body = server.call("/identity/rotate", payload)
    assert code == 409, body
    assert body["ok"] is False
    assert body["reason"] == reason
    assert body["fingerprint"] == before["fingerprint"]

    _, after = server.call("/identity")
    assert after["fingerprint"] == before["fingerprint"]
    assert server.app.hostkeys.fingerprint == before["fingerprint"]


def test_a_refused_rotation_is_audited(server):
    _, before = server.call("/identity")
    server.call("/identity/rotate", {})
    events = [e for e in server.app.db.audit_tail(200)
              if e["event"] == "identity_rotate_refused"]
    assert len(events) == 1
    assert events[0]["detail"]["reason"] == "rotation_unconfirmed"
    assert events[0]["detail"]["fingerprint"] == before["fingerprint"]


def test_rotation_is_audited_with_both_fingerprints(server):
    _, before = server.call("/identity")
    _, after = server.call(
        "/identity/rotate",
        {"confirm_fingerprint": before["fingerprint"]})
    events = [e for e in server.app.db.audit_tail(200)
              if e["event"] == "identity_rotated"]
    assert len(events) == 1
    assert events[0]["detail"]["previous_fingerprint"] == \
        before["fingerprint"]
    assert events[0]["detail"]["fingerprint"] == after["fingerprint"]


def test_a_failed_rotation_is_audited_and_never_silently_diverges(
        server, monkeypatch):
    """THE BLOCKER, end to end at the route. Previously: 500
    internal_error, NEITHER audit event, self.hostkeys unchanged, and
    the NEW key already on disk -- so the next boot silently adopted an
    identity nobody was told about and every peer pin broke."""
    _, before = server.call("/identity")
    failing = _FailingWrite(cp._write_private, hk.CERT_FILE)
    monkeypatch.setattr(cp, "_write_private", failing)
    code, body = server.call(
        "/identity/rotate",
        {"confirm_fingerprint": before["fingerprint"]})
    monkeypatch.undo()

    assert code == 409, body
    assert body["reason"] == "rotation_failed"
    # The refusal is TRUE: live, disk and the next boot all agree.
    assert server.app.hostkeys.fingerprint == before["fingerprint"]
    _, after = server.call("/identity")
    assert after["fingerprint"] == before["fingerprint"]
    assert hk.load_or_create(server.app.data_dir).fingerprint == \
        before["fingerprint"]

    events = [e for e in server.app.db.audit_tail(200)
              if e["event"].startswith("identity_rotate")]
    assert len(events) == 1
    assert events[0]["event"] == "identity_rotate_refused"
    assert events[0]["detail"]["reason"] == "rotation_failed"


def test_an_indeterminate_rotation_reports_what_is_actually_on_disk(
        server, monkeypatch):
    """The one case where the disk really moved. It must NOT read as a
    refusal, the live identity must be RE-READ so memory and disk agree,
    and the audit must say indeterminate."""
    _, before = server.call("/identity")
    real = cp._write_private
    state = {"cert_seen": False}

    def hostile(path, data):
        name = os.path.basename(path)
        if name == hk.CERT_FILE:
            state["cert_seen"] = True
            raise PermissionError(13, "cert locked")
        if name == hk.KEY_FILE and state["cert_seen"]:
            raise PermissionError(13, "rollback blocked")
        return real(path, data)

    monkeypatch.setattr(cp, "_write_private", hostile)
    code, body = server.call(
        "/identity/rotate",
        {"confirm_fingerprint": before["fingerprint"]})
    monkeypatch.undo()

    assert code == 500, body
    assert body["reason"] == "rotation_indeterminate"
    assert body["previous_fingerprint"] == before["fingerprint"]
    # MEMORY == DISK, whichever identity landed.
    on_disk = hk.load_or_create(server.app.data_dir).fingerprint
    assert body["fingerprint"] == on_disk
    assert server.app.hostkeys.fingerprint == on_disk
    _, served = server.call("/identity")
    assert served["fingerprint"] == on_disk

    events = [e for e in server.app.db.audit_tail(200)
              if e["event"] == "identity_rotate_indeterminate"]
    assert len(events) == 1
    assert events[0]["detail"]["previous_fingerprint"] == \
        before["fingerprint"]
    assert events[0]["detail"]["fingerprint"] == on_disk


def test_minting_is_audited_once(server):
    events = [e for e in server.app.db.audit_tail(200)
              if e["event"] == "identity_minted"]
    assert len(events) == 1
    assert events[0]["detail"]["fingerprint"] == \
        server.app.hostkeys.fingerprint


def test_the_fingerprint_is_audited_on_every_boot(tmp_path):
    """`origin == 'loaded'` only means a key file was PRESENT -- not
    that it is the same key. Auditing only the other origins left the
    single most audit-worthy event in the trust model unrecorded."""
    directory = str(tmp_path / "state")
    first = Server(directory)
    fingerprint = first.app.hostkeys.fingerprint
    first.stop()

    second = Server(directory)
    try:
        events = [e for e in second.app.db.audit_tail(200)
                  if e["event"].startswith("identity_")]
        assert [e["event"] for e in events] == ["identity_minted",
                                                "identity_loaded"]
        assert all(e["detail"]["fingerprint"] == fingerprint
                   for e in events)
    finally:
        second.stop()


def test_an_identity_that_changed_between_boots_is_audited(tmp_path):
    """Restoring the wrong backup, an operator swapping in another
    machine's identity.key, or a half-completed rotation all look like a
    normal 'loaded' start. PROVEN unrecorded by the panel; now it is the
    loudest line in the trail."""
    directory = str(tmp_path / "state")
    first = Server(directory)
    original = first.app.hostkeys.fingerprint
    first.stop()

    # Drop in a stranger's identity, key and matching cert -- exactly
    # what a wrong-backup restore leaves behind.
    stranger_dir = str(tmp_path / "stranger")
    stranger = hk.load_or_create(stranger_dir)
    for name in (hk.KEY_FILE, hk.CERT_FILE):
        with open(os.path.join(stranger_dir, name), "rb") as src:
            payload = src.read()
        with open(os.path.join(directory, name), "wb") as dst:
            dst.write(payload)

    second = Server(directory)
    try:
        assert second.app.hostkeys.fingerprint == stranger.fingerprint
        assert second.app.hostkeys.origin == "loaded"
        assert second.app.host_id == first.app.host_id, (
            "host_id is unchanged -- which is exactly why the "
            "fingerprint change has to be audited")
        changed = [e for e in second.app.db.audit_tail(200)
                   if e["event"] == "identity_changed"]
        assert len(changed) == 1
        assert changed[0]["detail"]["previous_fingerprint"] == original
        assert changed[0]["detail"]["fingerprint"] == stranger.fingerprint
    finally:
        second.stop()


def test_an_unchanged_identity_is_not_reported_as_changed(tmp_path):
    directory = str(tmp_path / "state")
    for _ in range(3):
        server = Server(directory)
        events = [e for e in server.app.db.audit_tail(200)
                  if e["event"] == "identity_changed"]
        server.stop()
        assert events == []


def test_unknown_identity_subpaths_are_not_routes(server):
    for path in ("/identity/", "/identity/key", "/identity/private",
                 "/identity/rotate/now"):
        code, body = server.call(path)
        assert code == 404, path
        assert body["reason"] == "not_found"


def test_a_corrupt_key_file_stops_the_host_app_from_starting(tmp_path):
    """The refusal is not merely a library nicety: HostApp must not come
    up on a damaged identity."""
    directory = str(tmp_path / "state")
    os.makedirs(directory)
    with open(os.path.join(directory, hk.KEY_FILE), "wb") as f:
        f.write(b"corrupt")
    with pytest.raises(hk.HostKeyError) as e:
        ha.HostApp(directory)
    assert e.value.reason == "identity_unreadable"


# -- identity survives a REAL process restart ----------------------------

def _start_host(directory):
    """Launch convoy_hostapp.py as a supervisor would, exactly as
    manual_exit_proof.py does, and wait for a LIVE portfile.

    NOT matched against Popen's pid, unlike manual_exit_proof.py. On
    Windows a virtualenv's Scripts/python.exe is a LAUNCHER that runs
    the base interpreter as a child, so Popen's pid is the launcher's
    and the portfile carries the real one (measured on this dev box:
    Popen 67812, portfile 74036). Terminating the launcher still takes
    the child with it, so shutdown is unaffected -- but a pid equality
    check would make this test pass only outside a venv. The portfile is
    cleared first, so 'live portfile present' cannot be satisfied by a
    stale one from an earlier boot.

    The real CLI intentionally has no artifact-cache override.  This test
    therefore replaces only the child process's HostApp class with a narrow
    subclass that injects a cache beneath its temporary data directory.  The
    daemon entry point and every production default remain unchanged, while
    the subprocess never writes to the developer's per-user cache.
    """
    cp.clear_portfile(directory)
    artifact_cache = os.path.join(directory, "artifact-cache")
    child_script = textwrap.dedent("""
        import sys

        sys.path.insert(0, %r)
        import convoy_hostapp as hostapp

        class IsolatedHostApp(hostapp.HostApp):
            def __init__(self, data_dir, *args, **kwargs):
                kwargs["artifact_cache_path"] = %r
                super().__init__(data_dir, *args, **kwargs)

        hostapp.HostApp = IsolatedHostApp
        sys.argv = ["convoy_hostapp.py", "--data-dir", %r]
        hostapp.main()
    """) % (HERE, artifact_cache, directory)
    process = subprocess.Popen(
        [sys.executable, "-c", child_script],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(160):
        if process.poll() is not None:
            raise AssertionError("host app exited: " + process.stderr.read())
        info = cp.read_live_portfile(directory)
        if info:
            return process, info["port"]
        time.sleep(0.25)
    process.kill()
    raise AssertionError("host app never wrote a live portfile")


def _stop_host(process, directory):
    """Stop the host app and WAIT until it is really gone, so the next
    boot cannot race the previous one for the data dir."""
    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)
    for _ in range(80):
        if cp.read_live_portfile(directory) is None:
            return
        time.sleep(0.25)
    raise AssertionError("the host app outlived its launcher")


def _call(directory, port, path, body=None):
    import urllib.request
    with open(os.path.join(directory, cp.TOKEN_FILE)) as f:
        token = f.read().strip()
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json",
                 ha.TOKEN_HEADER: token})
    with urllib.request.urlopen(request, timeout=15) as r:
        return json.loads(r.read().decode())


@pytest.mark.skipif(platform.python_implementation() != "CPython",
                    reason="spawns the host app with this interpreter")
def test_identity_survives_a_real_process_restart(tmp_path):
    """The manual_exit_proof pattern, as a test: two REAL processes on
    one data dir, killed the way a supervisor kills them. An identity
    that only survives a re-import proves nothing."""
    directory = str(tmp_path / "state")
    os.makedirs(directory)

    process, port = _start_host(directory)
    try:
        first = _call(directory, port, "/identity")
        assert hk.is_valid_fingerprint(first["fingerprint"])
    finally:
        _stop_host(process, directory)

    process2, port2 = _start_host(directory)
    try:
        second = _call(directory, port2, "/identity")
        assert second["fingerprint"] == first["fingerprint"]
        assert second["host_id"] == first["host_id"]
        assert second["alg"] == "ed25519-v1"

        # ... and a rotation in the SECOND process is durable into a third.
        rotated = _call(directory, port2, "/identity/rotate",
                        {"confirm_fingerprint": second["fingerprint"]})
        assert rotated["fingerprint"] != first["fingerprint"]
    finally:
        _stop_host(process2, directory)

    process3, port3 = _start_host(directory)
    try:
        third = _call(directory, port3, "/identity")
        assert third["fingerprint"] == rotated["fingerprint"]
    finally:
        _stop_host(process3, directory)


# -- graceful degradation when `cryptography` is absent ------------------

@pytest.fixture
def no_cryptography(monkeypatch):
    """Simulate the dependency being absent, exactly as the module's own
    captured import error would represent it."""
    monkeypatch.setattr(hk, "_IMPORT_ERROR",
                        ImportError("No module named 'cryptography'"))
    return None


def test_without_cryptography_the_module_reports_it_by_name(no_cryptography,
                                                            data_dir):
    assert hk.cryptography_available() is False
    with pytest.raises(hk.CryptographyMissing) as e:
        hk.load_or_create(data_dir)
    assert e.value.reason == "cryptography_missing"
    assert "cryptography" in e.value.detail
    # It is a HostKeyError too, so a caller that does not care can catch
    # the base -- but it is a DISTINCT type so the host app can catch
    # exactly this one and degrade.
    assert isinstance(e.value, hk.HostKeyError)
    # And nothing was written: no half-identity on disk.
    assert not os.path.exists(os.path.join(data_dir, hk.KEY_FILE))


def test_without_cryptography_rotation_and_verifiers_refuse(no_cryptography,
                                                            data_dir):
    with pytest.raises(hk.CryptographyMissing):
        hk.rotate(data_dir, "cvfp1-4t7k-9wqe-2m1x-bj0r-hd5n-6pza-c3vy-8sgf")
    with pytest.raises(hk.CryptographyMissing):
        hk.verifier_from_public_der(b"\x30\x2a")
    with pytest.raises(hk.CryptographyMissing):
        hk.Ed25519Signer(private_key=None, public_key=None)


def test_without_cryptography_the_host_app_still_serves_loopback(
        no_cryptography, tmp_path):
    """The whole point of degrading: a user on an older install must
    still get a working local host app, with the reason stated."""
    server = Server(str(tmp_path / "state"))
    try:
        assert server.app.hostkeys is None

        code, body = server.call("/health", token=None)
        assert code == 200 and body["ok"] is True

        code, body = server.call("/status")
        assert code == 200
        assert body["identity_alg"] is None
        assert body["identity_fingerprint"] is None
        assert body["identity_reason"] == "cryptography_missing"

        code, body = server.call("/identity")
        assert code == 503
        assert body["reason"] == "cryptography_missing"
        assert "cryptography" in body["detail"]
        assert body["host_id"] == server.app.host_id

        code, body = server.call("/identity/rotate", {})
        assert code == 503
        assert body["reason"] == "cryptography_missing"

        # Everything that does not need identity still works.
        code, body = server.call(
            "/register", {"project_root": "/Work/a", "convoy_id": "studio",
                          "comp_path": "/Embody"})
        assert code == 200 and body["ok"] is True
    finally:
        server.stop()


def test_the_module_imports_with_cryptography_genuinely_unavailable():
    """The monkeypatch above simulates absence; this proves the real
    thing. A child interpreter blocks the import at the meta-path level
    -- if convoy_hostkeys raised at import time, the whole daemon would
    be unimportable on an install that simply has not got the package."""
    script = textwrap.dedent("""
        import os, sys, tempfile

        class Block:
            def find_module(self, name, path=None):
                return self.find_spec(name, path)
            def find_spec(self, name, path=None, target=None):
                if name == "cryptography" or name.startswith("cryptography."):
                    raise ImportError("No module named 'cryptography'")
                return None

        sys.meta_path.insert(0, Block())
        for mod in [m for m in sys.modules if m.startswith("cryptography")]:
            del sys.modules[mod]

        sys.path.insert(0, %r)
        import convoy_hostkeys as hk
        assert hk.cryptography_available() is False
        import convoy_hostapp as ha
        directory = tempfile.mkdtemp(prefix="convoy_nocrypto_")
        app = ha.HostApp(
            directory,
            artifact_cache_path=os.path.join(directory, "artifact-cache"))
        assert app.hostkeys is None
        code, body = app.get_identity()
        assert code == 503, (code, body)
        assert body["reason"] == "cryptography_missing", body
        assert app.status()["identity_reason"] == "cryptography_missing"
        print("DEGRADED-OK")
    """) % HERE
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    assert "DEGRADED-OK" in result.stdout


# -- import isolation ----------------------------------------------------
#
# The dependency is scoped to ONE production module and ONE test module,
# and a scope enforced by a comment is not enforced at all.
#
# SCOPE, HONESTLY STATED. This is a HYGIENE guard against accidental
# drift -- a future module reaching for `cryptography` because it is
# already installed -- not an adversarial control. It walks the trees
# below RECURSIVELY and finds every static import plus the direct
# __import__/import_module string forms. It does NOT catch deliberately
# obfuscated ones (a name assembled at runtime, a variable passed to
# __import__, exec of a string), and it is not trying to: anybody able
# to land code in this repo can already do worse than import a library.
#
# The trees are the ones whose stdlib-only property is load-bearing:
#   dev/convoy         -- the host app, where the dependency lives
#   dev/embody         -- RECURSIVELY, because it contains the artifact
#                         A-44 actually binds: envoy_bridge.py and its
#                         shipped template twin text_envoy_bridge.py,
#                         plus Embody/convoy/ (the TD-side client) and
#                         the extensions that ride into a user's
#                         TouchDesigner install. The previous version
#                         walked neither the bridge nor Embody/, so
#                         `import cryptography` in envoy_bridge.py --
#                         the exact file the rule is about -- was green.
_SCANNED_TREES = ("dev/convoy", "dev/embody")

# ALLOWED BY REPO-RELATIVE PATH, never by basename. Matching the
# basename exempted any file merely NAMED convoy_hostkeys.py anywhere in
# the scan -- including dev/embody/Embody/convoy/, the shipped TD-side
# tree, which is the one place a third-party import would ride into a
# user's TouchDesigner install.
_CRYPTOGRAPHY_ALLOWED = {
    "dev/convoy/convoy_hostkeys.py",        # THE module that owns it
    "dev/convoy/test_convoy_hostkeys.py",   # its test, which must act as
                                            # an attacker holding raw
                                            # primitives (the domain
                                            # separation test would be
                                            # circular otherwise)
}

# THE VENDORED PAYLOAD, and why it is not simply added to the set above.
#
# dev/embody/Embody/convoy/host/ holds byte-identical copies of the host
# app, carried inside the .tox as text DATs so an install works offline
# (CONVOY_INSTALL_PLAN 1.1). convoy_hostkeys.py is one of them, so the
# scan sees `from cryptography import ...` inside the shipped TD-side
# tree -- the exact tree the comment above calls the one place a
# third-party import would ride into a user's install.
#
# Neither standard answer fits. "Move the import back behind
# convoy_hostkeys' API" is already true -- this IS that module, byte for
# byte. And a flat allowlist entry would be a PERMANENT, UNCONDITIONAL
# exemption for a path in the dangerous tree, which is precisely what
# keying by path instead of basename was meant to prevent.
#
# So the exemption is INHERITED and CONDITIONAL: a vendored file is
# exempt only while it is newline-normalized-identical to an ALLOWED
# original in dev/convoy/. The moment it diverges -- someone edits the
# vendored copy, or adds a module that is not a faithful copy -- it stops
# being "the same module" and is scanned like anything else.
# test_vendored_exemption_is_revoked_on_divergence pins that, so the
# exemption cannot quietly become an allowlist.
#
# What this deliberately does NOT claim: that shipping the file is
# harmless. `cryptography` IS absent from TouchDesigner's bundled
# interpreter (measured: TD 2025.33070 ships Python 3.11.15 without it),
# and the daemon degrades to identity_reason "cryptography_missing".
# TD itself never imports this file -- it is inert DAT text, written to
# disk at install time and executed by a separate interpreter.
_VENDOR_MIRROR_DIR = "dev/embody/Embody/convoy/host"
_VENDOR_SOURCE_DIR = "dev/convoy"


def _vendored_copy_of(name):
    """Repo-relative path of the dev/convoy original this file mirrors.

    Returns None unless `name` lives in the vendored payload dir AND its
    content matches the original exactly (newline-normalized: Embody
    writes CRLF on Windows while the sources are LF, and .gitattributes
    stores both as LF). A mismatch returns None, so the caller scans it.
    """
    prefix = _VENDOR_MIRROR_DIR + "/"
    if not name.startswith(prefix):
        return None
    origin = _VENDOR_SOURCE_DIR + "/" + name[len(prefix):]
    mirror_abs = os.path.join(REPO, *name.split("/"))
    origin_abs = os.path.join(REPO, *origin.split("/"))
    if not os.path.isfile(origin_abs):
        return None
    try:
        with open(mirror_abs, "rb") as fh:
            mirror = fh.read().replace(b"\r\n", b"\n")
        with open(origin_abs, "rb") as fh:
            source = fh.read().replace(b"\r\n", b"\n")
    except OSError:
        # Unreadable is NOT identical -- fail closed and scan it.
        return None
    return origin if mirror == source else None

# Per-tree floors. A single global floor was satisfied by dev/convoy
# alone, so losing an entire other tree -- a rename, a move, a typo in
# this list -- silently disabled that leg with the test still green.
_TREE_FLOORS = {"dev/convoy": 15, "dev/embody": 50}

_NO_REPO = ("no checkout root above this file (looked for a directory "
            "with pytest.ini and dev/) -- the import-isolation guard "
            "cannot run against a partial copy, and reporting a pass "
            "would be a lie")


def _relative(path):
    return os.path.relpath(path, REPO).replace(os.sep, "/")


def _python_files_under(*relative_dirs):
    """Every .py file under each tree, RECURSIVELY.

    os.listdir stopped at the top level, so anything in a subdirectory
    got zero enforcement -- and slice 3 adding dev/convoy/tls/ is a
    plausible near-term trigger. Returns (repo_relative_path, abs_path).
    """
    found = []
    for relative in relative_dirs:
        directory = os.path.join(REPO, *relative.split("/"))
        # ASSERT, never `continue`: a tree that has moved must fail the
        # guard loudly, not silently drop out of it.
        assert os.path.isdir(directory), (
            f"the import-isolation guard is configured to scan "
            f"{relative!r}, which does not exist -- fix _SCANNED_TREES "
            f"rather than letting the tree go unscanned")
        count = 0
        for root, dirs, names in os.walk(directory):
            dirs[:] = [d for d in sorted(dirs)
                       if d not in ("__pycache__", ".git")]
            for name in sorted(names):
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    found.append((_relative(path), path))
                    count += 1
        floor = _TREE_FLOORS.get(relative, 1)
        assert count >= floor, (
            f"{relative!r} yielded only {count} python files (expected "
            f">= {floor}) -- the scan has gone dark on that tree")
    return found


def _imports_cryptography(path):
    """A static import of `cryptography`, found structurally.

    AST rather than a text grep so a mention in a docstring or comment
    (this file is full of them) is not a false positive. The two direct
    dynamic forms are checked as well; see the scope note above for what
    is deliberately out of reach.
    """
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        with warnings.catch_warnings():
            # Parsing a tree this wide reaches legacy files with e.g.
            # invalid escape sequences. Their warnings belong to those
            # files, not to this guard, and must not turn a clean run
            # noisy (or fail one under -W error).
            warnings.simplefilter("ignore")
            tree = ast.parse(source, filename=path)
    except SyntaxError:
        # A template file that is not valid Python on its own cannot
        # import anything at runtime either.
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "cryptography" or \
                        alias.name.startswith("cryptography."):
                    return f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "cryptography" or \
                    module.startswith("cryptography."):
                return f"from {module} import ..."
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or \
                getattr(node.func, "attr", None)
            if name in ("__import__", "import_module"):
                for arg in node.args:
                    value = getattr(arg, "value", None)
                    if isinstance(value, str) and \
                            value.split(".")[0] == "cryptography":
                        return f"{name}({value!r})"
    return None


def test_import_isolation_no_other_module_imports_cryptography():
    """A-44's stdlib rule, made enforceable.

    If this fails, the first third-party dependency in the Convoy
    codebase (S14, a new supply-chain surface) has escaped the one
    module chartered to hold it. Move the import back behind
    convoy_hostkeys' API rather than widening the allowlist.
    """
    if REPO is None:
        pytest.skip(_NO_REPO)
    files = _python_files_under(*_SCANNED_TREES)
    names = {name for name, _ in files}
    assert "dev/convoy/convoy_hostkeys.py" in names
    # The artifact A-44 actually binds, named explicitly so it cannot
    # drop out of the scan unnoticed.
    for required in ("dev/embody/envoy_bridge.py",
                     "dev/embody/Embody/templates/text_envoy_bridge.py",
                     "dev/embody/Embody/convoy/convoy_client.py"):
        assert required in names, f"{required} is not being scanned"

    offenders = {}
    for name, path in files:
        if name in _CRYPTOGRAPHY_ALLOWED:
            continue
        # A vendored copy inherits its original's status, and ONLY while
        # it is still a faithful copy (see _vendored_copy_of).
        origin = _vendored_copy_of(name)
        if origin is not None and origin in _CRYPTOGRAPHY_ALLOWED:
            continue
        how = _imports_cryptography(path)
        if how:
            offenders[name] = how
    assert offenders == {}, (
        "only dev/convoy/convoy_hostkeys.py may take the `cryptography` "
        f"dependency; these modules also import it: {offenders}")


def test_vendored_exemption_is_revoked_on_divergence(tmp_path, monkeypatch):
    """The vendored exemption must be conditional, not an allowlist.

    A vendored copy is exempt only while it is byte-identical to an
    allowed original. If editing the vendored file kept the exemption,
    this would be a permanent hole in the shipped TD-side tree -- exactly
    what keying the allowlist by path rather than basename prevented.
    """
    if REPO is None:
        pytest.skip(_NO_REPO)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    source = tmp_path / "dev" / "convoy"
    mirror = tmp_path / "dev" / "embody" / "Embody" / "convoy" / "host"
    source.mkdir(parents=True)
    mirror.mkdir(parents=True)
    monkeypatch.setattr(sys.modules[__name__], "REPO", str(tmp_path))

    body = "from cryptography.hazmat.primitives import serialization\n"
    (source / "convoy_hostkeys.py").write_text(body)
    name = "dev/embody/Embody/convoy/host/convoy_hostkeys.py"

    # Faithful copy -> inherits the exemption.
    (mirror / "convoy_hostkeys.py").write_text(body)
    assert _vendored_copy_of(name) == "dev/convoy/convoy_hostkeys.py"

    # CRLF is a representation difference, not a divergence: Embody
    # writes CRLF on Windows and .gitattributes stores LF.
    (mirror / "convoy_hostkeys.py").write_bytes(
        body.replace("\n", "\r\n").encode())
    assert _vendored_copy_of(name) == "dev/convoy/convoy_hostkeys.py"

    # One added line -> no longer the same module -> exemption REVOKED.
    (mirror / "convoy_hostkeys.py").write_text(body + "import os\n")
    assert _vendored_copy_of(name) is None, (
        "an edited vendored copy must lose the exemption and be scanned")

    # A vendored file with no original at all is never exempt.
    (mirror / "convoy_invented.py").write_text(body)
    assert _vendored_copy_of(
        "dev/embody/Embody/convoy/host/convoy_invented.py") is None

    # And a faithful copy of a NON-allowed original stays scanned: the
    # inheritance carries the original's status, not a blanket pass.
    (source / "convoy_platform.py").write_text(body)
    (mirror / "convoy_platform.py").write_text(body)
    origin = _vendored_copy_of(
        "dev/embody/Embody/convoy/host/convoy_platform.py")
    assert origin == "dev/convoy/convoy_platform.py"
    assert origin not in _CRYPTOGRAPHY_ALLOWED, (
        "convoy_platform is not chartered to hold the dependency, so its "
        "vendored twin must not be exempt either")


def test_import_isolation_scans_subdirectories(tmp_path, monkeypatch):
    """The scan's own recursion, asserted. A planted import in a
    subdirectory used to pass -- os.listdir does not descend."""
    if REPO is None:
        pytest.skip(_NO_REPO)
    fake = tmp_path / "dev" / "convoy" / "tls"
    fake.mkdir(parents=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (fake / "helper.py").write_text("import cryptography\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO", str(tmp_path))
    monkeypatch.setitem(_TREE_FLOORS, "dev/convoy", 1)

    files = _python_files_under("dev/convoy")
    assert [n for n, _ in files] == ["dev/convoy/tls/helper.py"]
    assert _imports_cryptography(files[0][1])


def test_import_isolation_fails_loudly_if_a_scanned_tree_moves(tmp_path,
                                                               monkeypatch):
    """Silently skipping a missing tree is how an entire leg of the
    guard disappears. It must raise."""
    if REPO is None:
        pytest.skip(_NO_REPO)
    monkeypatch.setattr(sys.modules[__name__], "REPO", str(tmp_path))
    with pytest.raises(AssertionError) as e:
        _python_files_under("dev/embody")
    assert "does not exist" in str(e.value)


def test_import_isolation_allowlist_is_path_scoped_not_basename(tmp_path,
                                                                monkeypatch):
    """A file merely NAMED convoy_hostkeys.py in the SHIPPED TD-side
    tree used to be exempted automatically -- in the one directory where
    a third-party import rides into a user's TouchDesigner install."""
    shipped = tmp_path / "dev" / "embody" / "Embody" / "convoy"
    shipped.mkdir(parents=True)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (shipped / "convoy_hostkeys.py").write_text("import cryptography\n")
    monkeypatch.setattr(sys.modules[__name__], "REPO", str(tmp_path))
    monkeypatch.setitem(_TREE_FLOORS, "dev/embody", 1)

    files = _python_files_under("dev/embody")
    offenders = {n: _imports_cryptography(p) for n, p in files
                 if n not in _CRYPTOGRAPHY_ALLOWED
                 and _imports_cryptography(p)}
    assert offenders, (
        "a shipped module named convoy_hostkeys.py was exempted by "
        "basename")


def test_the_module_that_owns_the_dependency_actually_uses_it():
    """The other half of the isolation claim: the allowlist is not
    stale. If convoy_hostkeys stopped importing cryptography, the
    dependency would have moved somewhere unpoliced."""
    if REPO is None:
        pytest.skip(_NO_REPO)
    path = os.path.join(REPO, "dev", "convoy", "convoy_hostkeys.py")
    assert _imports_cryptography(path)


def test_the_host_app_reaches_identity_only_through_convoy_hostkeys():
    """Structural, not stylistic: the host app must never grow its own
    key handling beside the module that owns the policy."""
    if REPO is None:
        pytest.skip(_NO_REPO)
    path = os.path.join(REPO, "dev", "convoy", "convoy_hostapp.py")
    assert _imports_cryptography(path) is None
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "import convoy_hostkeys as hostkeys" in source
