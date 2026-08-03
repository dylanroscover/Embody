"""Pure tests for the Convoy hardware harness (no SSH or HostApp needed)."""

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


MODULE_PATH = pathlib.Path(__file__).with_name("convoy_hardware_e2e.py")
SPEC = importlib.util.spec_from_file_location("convoy_hardware_e2e", MODULE_PATH)
harness = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = harness
SPEC.loader.exec_module(harness)


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "tec-b4a ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestOnly\n",
            encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def config(self):
        return {
            "schema_version": 1,
            "convoy_id": "studio",
            "local": {},
            "remote": {
                "ssh_host": "tec-b4a",
                "ssh_port": 22,
                "ssh_user": "operator",
                "known_hosts_file": str(self.known_hosts),
                "identity_file": None,
                "python_command": "python",
                "target_node_id": "cvn_remote",
            },
            "timeouts": {},
            "scenarios": {
                "read": {"enabled": True, "operation": "query_network",
                         "arguments": {"parent_path": "/"}},
                "mutation": {"enabled": False},
            },
        }

    def write_config(self, value=None):
        path = self.root / "config.json"
        path.write_text(json.dumps(value or self.config()), encoding="utf-8")
        return path

    def test_config_defaults_and_relative_known_hosts_are_validated(self):
        value = self.config()
        value["remote"]["known_hosts_file"] = "known_hosts"
        loaded = harness.load_config(str(self.write_config(value)))
        self.assertEqual(loaded["convoy_id"], "studio")
        self.assertEqual(loaded["remote"]["ssh_host"], "tec-b4a")
        self.assertEqual(loaded["remote"]["known_hosts_file"],
                         str(self.known_hosts.resolve()))
        self.assertEqual(loaded["timeouts"]["job_s"], 90.0)

    def test_config_rejects_embedded_credentials_and_unsafe_host(self):
        for key, value in (("password", "bad"), ("host_token", "bad")):
            config = self.config()
            config["remote"][key] = value
            with self.subTest(key=key), self.assertRaises(harness.ConfigError):
                harness.load_config(str(self.write_config(config)))
        config = self.config()
        config["remote"]["ssh_host"] = "-oProxyCommand=evil"
        with self.assertRaises(harness.ConfigError):
            harness.load_config(str(self.write_config(config)))

    def test_ssh_command_is_always_strict_and_never_accept_new(self):
        config = harness.load_config(str(self.write_config()))
        argv = harness.build_ssh_argv(
            config["remote"], "python -V", 7.2)
        joined = " ".join(argv)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("UserKnownHostsFile=", joined)
        self.assertNotIn("accept-new", joined)
        self.assertNotIn("StrictHostKeyChecking=no", joined)
        self.assertEqual(argv[-2], "operator@tec-b4a")

    def test_redaction_is_recursive_and_replaces_runtime_token_values(self):
        token = "super-secret-runtime-token"
        value = {
            "token": token,
            "nested": [{"Authorization": "Bearer " + token},
                       "prefix " + token + " suffix"],
            "safe": 3,
        }
        result = harness.redact(value, [token])
        encoded = json.dumps(result)
        self.assertNotIn(token, encoded)
        self.assertEqual(result["token"], "[REDACTED]")
        self.assertEqual(result["safe"], 3)

    def test_json_and_junit_result_shapes_include_skips_and_no_secrets(self):
        token = "runtime-token-never-report"
        recorder = harness.ResultRecorder("run-1", secret_values=[token])
        recorder.add("ok", "preflight", "passed", 0.1,
                     details={"host": "h"})
        recorder.add("optional", "scenario", "skipped", 0.2,
                     "needs flag", {"host_token": token})
        recorder.add("bad", "scenario", "failed", 0.3,
                     "response contained " + token)
        json_path = self.root / "result.json"
        junit_path = self.root / "result.xml"
        harness.write_results(recorder, str(json_path), str(junit_path),
                              {"mode": "test"})

        raw = json_path.read_text(encoding="utf-8")
        self.assertNotIn(token, raw)
        document = json.loads(raw)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["summary"], {
            "passed": 1, "failed": 1, "skipped": 1,
            "error": 0, "total": 3})
        suite = ET.parse(junit_path).getroot()
        self.assertEqual(suite.attrib["tests"], "3")
        self.assertEqual(suite.attrib["failures"], "1")
        self.assertEqual(suite.attrib["skipped"], "1")
        self.assertNotIn(token, junit_path.read_text(encoding="utf-8"))

    def test_default_selection_is_nondestructive(self):
        self.assertEqual(harness.select_scenarios(None), ("ping", "read"))
        self.assertTrue(set(harness.select_scenarios(["all"]))
                        .issuperset(harness.MUTATING_SCENARIOS))

    def test_mutation_and_disruption_are_independent_explicit_gates(self):
        config = harness.load_config(str(self.write_config()))
        runner = harness.ConvoyHardwareRunner(
            config, (), False, False, harness.ResultRecorder("run"))
        with self.assertRaises(harness.SkipScenario):
            runner._require_safety("mutation")
        with self.assertRaises(harness.SkipScenario):
            runner._require_safety("dropout")
        runner = harness.ConvoyHardwareRunner(
            config, (), True, False, harness.ResultRecorder("run"))
        runner._require_safety("mutation")
        with self.assertRaises(harness.SkipScenario):
            runner._require_safety("dropout")
        runner = harness.ConvoyHardwareRunner(
            config, (), True, True, harness.ResultRecorder("run"))
        runner._require_safety("dropout")


if __name__ == "__main__":
    unittest.main()
