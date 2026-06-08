"""Headless helpers for reading and querying the bundled Specimen manifest.

This module mirrors the frozen C3 manifest contract without importing
TouchDesigner. It is safe to use from tests and build scripts.
"""
from __future__ import annotations

import json
import re

DIFFICULTIES = ("starter", "intermediate", "advanced")
REQUIRED_FIELDS = (
    "slug",
    "name",
    "category",
    "difficulty",
    "description",
    "requires",
    "output_op",
    "tdn_path",
    "operator_count",
)
SLIM_FIELDS = (
    "slug",
    "name",
    "category",
    "difficulty",
    "description",
    "requires",
    "operator_count",
    "tdn_path",
    "thumbnail_path",
)
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def load_manifest(path: str) -> dict:
    """Load a manifest JSON file and validate the top-level shape."""
    with open(path, "r", encoding="utf-8") as infile:
        manifest = json.load(infile)

    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a dict")
    if not isinstance(manifest.get("version"), str):
        raise ValueError('manifest must include a "version" string')
    if not isinstance(manifest.get("specimens"), list):
        raise ValueError('manifest must include a "specimens" list')
    return manifest


def list_specimens(
    manifest: dict,
    category: str | None = None,
    tags: list | None = None,
    difficulty: str | None = None,
    requires: str | None = None,
) -> list[dict]:
    """Return slim specimen records matching all requested filters."""
    requested_tags = tags or []
    specimens = _manifest_specimens(manifest)
    records = []

    for entry in specimens:
        if not isinstance(entry, dict):
            continue
        if category is not None and entry.get("category") != category:
            continue
        if difficulty is not None and entry.get("difficulty") != difficulty:
            continue
        if requires is not None and entry.get("requires") != requires:
            continue

        entry_tags = entry.get("tags")
        if not isinstance(entry_tags, list):
            entry_tags = []
        if any(tag not in entry_tags for tag in requested_tags):
            continue

        records.append({field: entry.get(field) for field in SLIM_FIELDS})

    return records


def get_specimen(manifest: dict, slug: str) -> dict | None:
    """Return the full specimen entry for slug, or None if it is absent."""
    for entry in _manifest_specimens(manifest):
        if isinstance(entry, dict) and entry.get("slug") == slug:
            return entry
    return None


def validate_manifest_entry(entry: dict) -> list[str]:
    """Return human-readable C3 contract problems for one manifest entry."""
    if not isinstance(entry, dict):
        return ["entry must be a dict"]

    problems = []
    for field in REQUIRED_FIELDS:
        if field not in entry:
            problems.append("missing required field: " + field)

    if "output_op" in entry and entry.get("output_op") != "out":
        problems.append('output_op must be "out"')

    if "difficulty" in entry and entry.get("difficulty") not in DIFFICULTIES:
        problems.append(
            "difficulty must be one of: " + ", ".join(DIFFICULTIES)
        )

    if "slug" in entry:
        slug = entry.get("slug")
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            problems.append(
                "slug must match ^[a-z0-9]+(-[a-z0-9]+)*$"
            )

    if "operator_count" in entry:
        operator_count = entry.get("operator_count")
        if (
            not isinstance(operator_count, int)
            or isinstance(operator_count, bool)
            or operator_count < 1
        ):
            problems.append("operator_count must be an int >= 1")

    return problems


def validate_manifest(manifest: dict) -> dict:
    """Validate every manifest entry and report duplicate slugs."""
    if not isinstance(manifest, dict):
        return {
            "ok": False,
            "errors": [
                {
                    "slug_or_index": "manifest",
                    "problems": ["manifest must be a dict"],
                }
            ],
        }

    specimens = manifest.get("specimens")
    if not isinstance(specimens, list):
        return {
            "ok": False,
            "errors": [
                {
                    "slug_or_index": "manifest",
                    "problems": ['manifest must include a "specimens" list'],
                }
            ],
        }

    errors = []
    seen_slugs = set()
    for index, entry in enumerate(specimens):
        problems = validate_manifest_entry(entry)

        slug = entry.get("slug") if isinstance(entry, dict) else None
        if isinstance(slug, str):
            if slug in seen_slugs:
                problems.append("duplicate slug: " + slug)
            else:
                seen_slugs.add(slug)

        if problems:
            errors.append(
                {
                    "slug_or_index": slug if slug is not None else index,
                    "problems": problems,
                }
            )

    return {"ok": not errors, "errors": errors}


def _manifest_specimens(manifest: dict) -> list:
    if not isinstance(manifest, dict):
        return []
    specimens = manifest.get("specimens")
    if not isinstance(specimens, list):
        return []
    return specimens
