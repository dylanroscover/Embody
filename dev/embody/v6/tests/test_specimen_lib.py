import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import specimen_lib


class TestSpecimenLib(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "version": "1",
            "specimens": [
                {
                    "slug": "basic-noise",
                    "name": "Basic Noise",
                    "category": "TOP",
                    "difficulty": "starter",
                    "description": "Noise to out.",
                    "requires": "none",
                    "output_op": "out",
                    "tdn_path": "specimens/basic-noise.tdn",
                    "thumbnail_path": "specimens/basic-noise.png",
                    "operator_count": 3,
                    "tags": ["noise", "top", "starter"],
                },
                {
                    "slug": "audio-reactive",
                    "name": "Audio Reactive",
                    "category": "CHOP",
                    "difficulty": "intermediate",
                    "description": "Audio drives a visual.",
                    "requires": "Essentia",
                    "output_op": "out",
                    "tdn_path": "specimens/audio-reactive.tdn",
                    "operator_count": 8,
                    "tags": ["audio", "top", "analysis"],
                },
                {
                    "slug": "raytk-shape",
                    "name": "RayTK Shape",
                    "category": "RayTK",
                    "difficulty": "advanced",
                    "description": "RayTK scene to out.",
                    "requires": "RayTK",
                    "output_op": "out",
                    "tdn_path": "specimens/raytk-shape.tdn",
                    "thumbnail_path": None,
                    "operator_count": 12,
                    "tags": ["raytk", "sdf", "top"],
                },
                {
                    "slug": "table-maker",
                    "name": "Table Maker",
                    "category": "DAT",
                    "difficulty": "starter",
                    "description": "DAT table output.",
                    "requires": "none",
                    "output_op": "out",
                    "tdn_path": "specimens/table-maker.tdn",
                    "operator_count": 4,
                    "tags": ["dat", "starter"],
                },
            ],
        }

    def test_list_specimens_filters_by_category(self):
        records = specimen_lib.list_specimens(self.manifest, category="TOP")

        self.assertEqual(["basic-noise"], [record["slug"] for record in records])

    def test_list_specimens_filters_by_difficulty(self):
        records = specimen_lib.list_specimens(
            self.manifest,
            difficulty="starter",
        )

        self.assertEqual(
            ["basic-noise", "table-maker"],
            [record["slug"] for record in records],
        )

    def test_list_specimens_filters_by_requires(self):
        records = specimen_lib.list_specimens(self.manifest, requires="RayTK")

        self.assertEqual(["raytk-shape"], [record["slug"] for record in records])

    def test_list_specimens_filters_by_tags_requiring_all_tags(self):
        records = specimen_lib.list_specimens(
            self.manifest,
            tags=["top", "analysis"],
        )

        self.assertEqual(["audio-reactive"], [record["slug"] for record in records])

    def test_list_specimens_combines_filters(self):
        records = specimen_lib.list_specimens(
            self.manifest,
            category="DAT",
            tags=["starter"],
            difficulty="starter",
            requires="none",
        )

        self.assertEqual(["table-maker"], [record["slug"] for record in records])

    def test_list_specimens_returns_exact_slim_keys_and_none_for_absent(self):
        records = specimen_lib.list_specimens(self.manifest)

        self.assertEqual(
            {
                "slug",
                "name",
                "category",
                "difficulty",
                "description",
                "requires",
                "operator_count",
                "tdn_path",
                "thumbnail_path",
            },
            set(records[1].keys()),
        )
        self.assertIsNone(records[1]["thumbnail_path"])

    def test_get_specimen_returns_hit_and_miss(self):
        self.assertEqual(
            "Audio Reactive",
            specimen_lib.get_specimen(self.manifest, "audio-reactive")["name"],
        )
        self.assertIsNone(specimen_lib.get_specimen(self.manifest, "missing"))

    def test_validate_manifest_entry_reports_missing_field(self):
        entry = dict(self.manifest["specimens"][0])
        del entry["tdn_path"]

        problems = specimen_lib.validate_manifest_entry(entry)

        self.assertIn("missing required field: tdn_path", problems)

    def test_validate_manifest_entry_reports_wrong_difficulty(self):
        entry = dict(self.manifest["specimens"][0])
        entry["difficulty"] = "expert"

        problems = specimen_lib.validate_manifest_entry(entry)

        self.assertIn(
            "difficulty must be one of: starter, intermediate, advanced",
            problems,
        )

    def test_validate_manifest_entry_reports_wrong_output_op(self):
        entry = dict(self.manifest["specimens"][0])
        entry["output_op"] = "null1"

        problems = specimen_lib.validate_manifest_entry(entry)

        self.assertIn('output_op must be "out"', problems)

    def test_validate_manifest_entry_reports_bad_slug(self):
        entry = dict(self.manifest["specimens"][0])
        entry["slug"] = "Bad Slug"

        problems = specimen_lib.validate_manifest_entry(entry)

        self.assertIn("slug must match ^[a-z0-9]+(-[a-z0-9]+)*$", problems)

    def test_validate_manifest_flags_duplicate_slugs(self):
        manifest = {
            "version": "1",
            "specimens": [
                dict(self.manifest["specimens"][0]),
                dict(self.manifest["specimens"][0]),
            ],
        }

        result = specimen_lib.validate_manifest(manifest)

        self.assertFalse(result["ok"])
        self.assertEqual("basic-noise", result["errors"][0]["slug_or_index"])
        self.assertIn("duplicate slug: basic-noise", result["errors"][0]["problems"])

    def test_validate_manifest_accepts_valid_manifest(self):
        result = specimen_lib.validate_manifest(self.manifest)

        self.assertTrue(result["ok"])
        self.assertEqual([], result["errors"])


if __name__ == "__main__":
    unittest.main()
