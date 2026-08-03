"""Shared HTTP wire rules for Convoy artifact transfer.

Artifact *content* is always an HTTP byte stream.  JSON is used only for
small metadata records and capability grants.  This module deliberately owns
the path/header/range grammar so the loopback and mutual-TLS listeners cannot
quietly implement two different protocols.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.parse

import convoy_identity as identity


LOCAL_ROUTE_PREFIX = "/artifacts/"
PEER_ROUTE_PREFIX = "/peer/artifacts/"

HEADER_SHA256 = "X-Convoy-Content-SHA256"
HEADER_NODE_ID = "X-Convoy-Node-ID"
HEADER_CONTROLLER_ID = "X-Convoy-Controller-ID"
HEADER_FILENAME = "X-Convoy-Filename"
HEADER_CAPABILITY = "X-Convoy-Artifact-Capability"

DEFAULT_MAX_TRANSFERS = 4
STREAM_CHUNK_BYTES = 64 * 1024
MAX_HEADER_TEXT_BYTES = 256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{64}$")
_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)$")


class ArtifactHTTPError(Exception):
    """A stable, structured HTTP refusal."""

    def __init__(self, status, reason, detail="", headers=None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.status = int(status)
        self.reason = reason
        self.detail = detail
        self.headers = dict(headers or {})

    def payload(self):
        result = {"ok": False, "reason": self.reason}
        if self.detail:
            result["detail"] = self.detail
        return result


class LimitedReader:
    """Expose exactly Content-Length bytes from a persistent HTTP stream.

    ArtifactStore performs one final ``read(1)`` to prove the source ended.
    Reading the handler's raw ``rfile`` there would block waiting for another
    HTTP request.  This wrapper returns EOF at the declared boundary without
    ever reading beyond it.
    """

    def __init__(self, stream, length):
        self._stream = stream
        self.remaining = int(length)

    def read(self, size=-1):
        if self.remaining <= 0:
            return b""
        if size is None or size < 0:
            size = self.remaining
        amount = min(int(size), self.remaining)
        if amount <= 0:
            return b""
        block = self._stream.read(amount)
        if block:
            self.remaining -= len(block)
        return block

    def discard(self):
        """Drain the declared body, bounded by the already-validated size."""
        while self.remaining:
            block = self.read(min(STREAM_CHUNK_BYTES, self.remaining))
            if not block:
                break
        return self.remaining == 0


def encode_convoy_segment(convoy_id):
    """Canonical base64url path segment for one normalized Convoy id."""
    try:
        normalized = identity.normalize_convoy_id(convoy_id)
        raw = normalized.encode("utf-8")
    except (identity.IdentityError, UnicodeEncodeError):
        raise ArtifactHTTPError(
            400, "artifact_invalid", "invalid Convoy namespace")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_convoy_segment(segment):
    if (not isinstance(segment, str) or not segment or "/" in segment
            or "?" in segment or "#" in segment or "=" in segment):
        raise ArtifactHTTPError(
            400, "artifact_invalid", "invalid Convoy namespace segment")
    maximum = (identity.MAX_CONVOY_ID_BYTES * 4 + 2) // 3
    if len(segment) > maximum:
        raise ArtifactHTTPError(
            400, "artifact_invalid", "invalid Convoy namespace segment")
    try:
        encoded = segment.encode("ascii")
        raw = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_", validate=True)
        text = raw.decode("utf-8")
        normalized = identity.normalize_convoy_id(text)
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError,
            identity.IdentityError):
        raise ArtifactHTTPError(
            400, "artifact_invalid", "invalid Convoy namespace segment")
    canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    if canonical != segment or normalized != text:
        raise ArtifactHTTPError(
            400, "artifact_invalid", "non-canonical Convoy namespace segment")
    return normalized


def parse_route(raw_path, prefix):
    """Return ``(action, convoy_id, artifact_id)`` or ``None``.

    Actions are ``upload``, ``download`` and ``capability``.  Artifact routes
    accept no query string: bearer capabilities stay in a header rather than
    URLs, logs, browser history, or proxy request lines.
    """
    parsed = urllib.parse.urlsplit(raw_path)
    if not parsed.path.startswith(prefix):
        return None
    if parsed.query or parsed.fragment:
        raise ArtifactHTTPError(
            400, "artifact_invalid", "artifact routes do not accept queries")
    remainder = parsed.path[len(prefix):]
    parts = remainder.split("/") if remainder else []
    if not parts or any(not part for part in parts):
        raise ArtifactHTTPError(404, "not_found")
    convoy_id = decode_convoy_segment(parts[0])
    if len(parts) == 1:
        return "upload", convoy_id, None
    artifact_id = parts[1]
    if not _ARTIFACT_ID.fullmatch(artifact_id):
        raise ArtifactHTTPError(400, "artifact_invalid", "malformed artifact_id")
    if len(parts) == 2:
        return "download", convoy_id, artifact_id
    if len(parts) == 3 and parts[2] == "capability":
        return "capability", convoy_id, artifact_id
    raise ArtifactHTTPError(404, "not_found")


def _single_header(headers, name, required=True):
    values = headers.get_all(name) if hasattr(headers, "get_all") else None
    if values is None:
        value = headers.get(name)
        values = [] if value is None else [value]
    if len(values) > 1:
        raise ArtifactHTTPError(
            400, "artifact_invalid", f"multiple {name} headers")
    if not values:
        if required:
            raise ArtifactHTTPError(
                400, "artifact_invalid", f"missing {name} header")
        return ""
    value = values[0]
    if not isinstance(value, str):
        raise ArtifactHTTPError(
            400, "artifact_invalid", f"invalid {name} header")
    return value


def bounded_header(headers, name, required=True):
    value = _single_header(headers, name, required=required)
    if not value and not required:
        return ""
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise ArtifactHTTPError(
            400, "artifact_invalid", f"invalid {name} header")
    if (not value or value != value.strip()
            or len(encoded) > MAX_HEADER_TEXT_BYTES
            or any(byte < 32 or byte == 127 for byte in encoded)):
        raise ArtifactHTTPError(
            400, "artifact_invalid", f"invalid {name} header")
    return value


def upload_metadata(headers, max_artifact_bytes):
    """Validate upload framing before the handler reads a single byte."""
    transfer_encoding = _single_header(
        headers, "Transfer-Encoding", required=False)
    if transfer_encoding:
        raise ArtifactHTTPError(
            400, "artifact_invalid", "Transfer-Encoding is not supported")
    content_length = _single_header(headers, "Content-Length")
    if not _DECIMAL.fullmatch(content_length):
        raise ArtifactHTTPError(
            400, "artifact_invalid", "invalid Content-Length")
    size = int(content_length)
    if size > int(max_artifact_bytes):
        raise ArtifactHTTPError(
            413, "artifact_too_large", "upload exceeds per-artifact limit")
    digest = _single_header(headers, HEADER_SHA256)
    if not _SHA256.fullmatch(digest):
        raise ArtifactHTTPError(
            400, "artifact_invalid", "content SHA-256 must be lowercase hex")
    mime_type = _single_header(headers, "Content-Type", required=False)
    mime_type = mime_type or "application/octet-stream"
    filename = bounded_header(headers, HEADER_FILENAME, required=False)
    node_id = bounded_header(headers, HEADER_NODE_ID)
    controller_id = bounded_header(headers, HEADER_CONTROLLER_ID)
    return {
        "expected_size": size,
        "expected_sha256": digest,
        "mime_type": mime_type,
        "filename_hint": filename or None,
        "node_id": node_id,
        "controller_id": controller_id,
    }


def capability_from_headers(headers):
    return bounded_header(headers, HEADER_CAPABILITY)


def range_from_headers(headers):
    return _single_header(headers, "Range", required=False) or None


def parse_range(value, total_size):
    """Parse one RFC 7233 byte range.

    Returns ``(offset, length, partial)``.  Multi-range responses are refused;
    Convoy's resume contract only needs one contiguous suffix/window and this
    keeps memory and response framing deterministic.
    """
    if value is None or value == "":
        return 0, total_size, False
    unsatisfied = {"Content-Range": f"bytes */{total_size}"}
    if not isinstance(value, str) or not value.startswith("bytes="):
        raise ArtifactHTTPError(
            416, "artifact_range_invalid", "only byte ranges are supported",
            unsatisfied)
    spec = value[6:]
    if not spec or "," in spec or spec.count("-") != 1:
        raise ArtifactHTTPError(
            416, "artifact_range_invalid", "exactly one range is required",
            unsatisfied)
    first, last = spec.split("-", 1)
    try:
        if first:
            if not _DECIMAL.fullmatch(first):
                raise ValueError
            start = int(first)
            if start >= total_size:
                raise ValueError
            if last:
                if not _DECIMAL.fullmatch(last):
                    raise ValueError
                end = min(int(last), total_size - 1)
                if end < start:
                    raise ValueError
            else:
                end = total_size - 1
        else:
            if not last or not _DECIMAL.fullmatch(last):
                raise ValueError
            suffix = int(last)
            if suffix <= 0 or total_size <= 0:
                raise ValueError
            start = max(0, total_size - suffix)
            end = total_size - 1
    except (TypeError, ValueError):
        raise ArtifactHTTPError(
            416, "artifact_range_invalid", "range is not satisfiable",
            unsatisfied)
    return start, end - start + 1, True


def peer_audience(host_id, convoy_id, node_id, controller_id):
    """Opaque exact binding for one peer/controller/node download grant."""
    material = json.dumps(
        [host_id, convoy_id, node_id, controller_id],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "artifact-peer-v1-" + hashlib.sha256(material).hexdigest()


__all__ = [
    "ArtifactHTTPError", "LimitedReader", "LOCAL_ROUTE_PREFIX",
    "PEER_ROUTE_PREFIX", "HEADER_SHA256", "HEADER_NODE_ID",
    "HEADER_CONTROLLER_ID", "HEADER_FILENAME", "HEADER_CAPABILITY",
    "DEFAULT_MAX_TRANSFERS", "STREAM_CHUNK_BYTES", "encode_convoy_segment",
    "decode_convoy_segment", "parse_route", "bounded_header",
    "upload_metadata", "capability_from_headers", "range_from_headers",
    "parse_range",
    "peer_audience",
]
