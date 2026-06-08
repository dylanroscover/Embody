import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import contracts
import tdn_envelope


class TestTdnEnvelope(unittest.TestCase):
    def test_wrap_produces_valid_envelope_without_optional_fields(self):
        tdn = {"operators": [{"name": "text1", "type": "textDAT"}]}

        envelope = tdn_envelope.wrap_tdn(tdn, "embody")

        self.assertTrue(contracts.is_embody_tdn_envelope(envelope))
        self.assertEqual(
            envelope[contracts.EMBODY_TDN_MARKER],
            contracts.EMBODY_TDN_VERSION,
        )
        self.assertEqual(envelope["source"], "embody")
        self.assertEqual(envelope["sha256"], tdn_envelope.tdn_sha256(tdn))
        self.assertIs(envelope["tdn"], tdn)
        self.assertNotIn("slug", envelope)
        self.assertNotIn("version", envelope)

    def test_wrap_includes_optional_fields_when_given(self):
        tdn = {"operators": []}

        envelope = tdn_envelope.wrap_tdn(
            tdn,
            "embody.tools",
            slug="sample-network",
            version=7,
        )

        self.assertTrue(contracts.is_embody_tdn_envelope(envelope))
        self.assertEqual(envelope["source"], "embody.tools")
        self.assertEqual(envelope["slug"], "sample-network")
        self.assertEqual(envelope["version"], 7)

    def test_clipboard_round_trip_returns_equal_envelope(self):
        envelope = tdn_envelope.wrap_tdn(
            {"b": 2, "a": {"name": "base1"}},
            "embody",
            slug="round-trip",
            version=1,
        )

        text = tdn_envelope.to_clipboard_str(envelope)
        unwrapped = tdn_envelope.unwrap_clipboard(text)

        self.assertEqual(unwrapped, envelope)

    def test_unwrap_malformed_json_returns_none(self):
        self.assertIsNone(tdn_envelope.unwrap_clipboard("not json"))

    def test_unwrap_non_envelope_json_returns_none(self):
        self.assertIsNone(tdn_envelope.unwrap_clipboard('{"a":1}'))

    def test_tdn_sha256_is_deterministic_regardless_of_key_order(self):
        first = {"b": 2, "a": {"d": 4, "c": 3}}
        second = {"a": {"c": 3, "d": 4}, "b": 2}

        self.assertEqual(
            tdn_envelope.tdn_sha256(first),
            tdn_envelope.tdn_sha256(second),
        )

    def test_verify_envelope_integrity_detects_mutation(self):
        envelope = tdn_envelope.wrap_tdn({"operators": [{"name": "a"}]}, "embody")

        self.assertTrue(tdn_envelope.verify_envelope_integrity(envelope))

        envelope["tdn"]["operators"][0]["name"] = "b"

        self.assertFalse(tdn_envelope.verify_envelope_integrity(envelope))

    def test_wrap_raises_value_error_on_bad_source(self):
        with self.assertRaises(ValueError):
            tdn_envelope.wrap_tdn({"operators": []}, "other")


if __name__ == "__main__":
    unittest.main()
