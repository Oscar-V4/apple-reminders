from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_external_tester_receipt as validator

SCHEMA = json.loads((ROOT / "docs/launch/external-tester-receipt.schema.json").read_text())
EXAMPLE = ROOT / "docs/launch/examples/fresh-native-image-no-clt.synthetic.example.json"


class FreshNativeReceiptTests(unittest.TestCase):
    def receipt(self):
        return json.loads(EXAMPLE.read_text())

    def valid(self, receipt):
        validator.validate_receipt(receipt, SCHEMA)

    def invalid(self, receipt):
        with self.assertRaises(validator.ReceiptError):
            self.valid(receipt)

    def check(self, receipt, name):
        return next(check for check in receipt["checks"] if check["id"] == name)

    def stop_at(self, receipt, name, outcome="blocked", error="capability_unavailable"):
        checks = receipt["checks"]
        index = next(index for index, check in enumerate(checks) if check["id"] == name)
        checks[index].update(outcome=outcome, error_category=error)
        for check in checks[index + 1:]:
            check.update(outcome="not_run", error_category="not_run")
        receipt["exact_cleanup"] = "not_run"
        if index <= validator.FRESH_NATIVE_CHECKS.index("permission_allow"):
            receipt["tcc_result"] = "not_checked"
        receipt["native_test_context"]["fixture_state"] = (
            "not_created" if index < validator.FRESH_NATIVE_CHECKS.index("synthetic_fixture_create") else "unknown"
        )

    def test_synthetic_success_uses_ten_closed_checks(self):
        receipt = self.receipt()
        self.assertEqual(len(receipt["checks"]), 10)
        for subject in ("fresh_macos_user", "fresh_vm"):
            receipt["native_test_context"]["subject"] = subject
            self.valid(receipt)
        # Bundled use does not imply absence of another interpreter.
        for value in (None, "absent", "installed", "not_checked"):
            if value is None:
                receipt.pop("external_python", None)
            else:
                receipt["external_python"] = value
            self.valid(receipt)

    def test_honest_stops_at_each_prerequisite_validate_without_cleanup_fiction(self):
        for name in validator.FRESH_NATIVE_CHECKS[:-1]:
            for outcome, category in (("failed", "unexpected_failure"), ("blocked", "unsupported_build"), ("not_run", "not_run")):
                with self.subTest(check=name, outcome=outcome):
                    receipt = self.receipt()
                    self.stop_at(receipt, name, outcome, category)
                    if name == "synthetic_fixture_create" and outcome == "not_run":
                        receipt["native_test_context"]["fixture_state"] = "not_created"
                    self.valid(receipt)

    def test_unavailable_helper_and_unknown_schema_cannot_be_followed_by_mutation(self):
        for name, error in (("bundled_native_runtime", "helper_unavailable"), ("experimental_capability", "schema_unverified")):
            receipt = self.receipt()
            self.stop_at(receipt, name, error=error)
            self.valid(receipt)
            self.check(receipt, "experimental_synthetic_mutation").update(outcome="passed", error_category="none")
            self.invalid(receipt)

    def test_permission_denial_and_unconfirmed_result_are_valid_stops(self):
        receipt = self.receipt()
        self.stop_at(receipt, "permission_allow", error="permission_denied")
        receipt["tcc_result"] = "denied"
        self.valid(receipt)
        receipt["tcc_result"] = "granted_after_prompt"
        self.invalid(receipt)
        receipt = self.receipt()
        self.stop_at(receipt, "permission_allow", outcome="failed", error="unexpected_failure")
        self.valid(receipt)

    def test_failed_creation_requires_independent_fixture_knowledge(self):
        receipt = self.receipt()
        self.stop_at(receipt, "synthetic_fixture_create", outcome="failed", error="verification_pending")
        self.valid(receipt)  # Unknown creation; no automatic cleanup claim.
        receipt["native_test_context"]["fixture_state"] = "not_created"
        self.invalid(receipt)
        receipt["native_test_context"]["fixture_state"] = "verified_absent"
        receipt["exact_cleanup"] = "passed"
        self.check(receipt, "exact_cleanup").update(outcome="passed", error_category="none")
        self.valid(receipt)  # Separate exact read proves absence after uncertainty.

    def test_failed_mutation_does_not_force_cleanup_success(self):
        for state in ("known_retained", "unknown"):
            for cleanup in ("failed", "not_run"):
                receipt = self.receipt()
                self.stop_at(receipt, "experimental_synthetic_mutation", outcome="failed", error="partial_success")
                receipt["native_test_context"]["fixture_state"] = state
                receipt["exact_cleanup"] = cleanup
                self.check(receipt, "exact_cleanup").update(outcome=cleanup, error_category="cleanup_failed" if cleanup == "failed" else "not_run")
                self.valid(receipt)
                receipt["exact_cleanup"] = "passed"
                self.check(receipt, "exact_cleanup").update(outcome="passed", error_category="none")
                self.invalid(receipt)

    def test_cleanup_and_creation_states_are_consistent(self):
        for state in ("not_created", "known_retained", "unknown"):
            receipt = self.receipt()
            receipt["native_test_context"]["fixture_state"] = state
            self.invalid(receipt)
        receipt = self.receipt()
        self.stop_at(receipt, "experimental_capability")
        receipt["native_test_context"]["fixture_state"] = "verified_absent"
        receipt["exact_cleanup"] = "passed"
        self.check(receipt, "exact_cleanup").update(outcome="passed", error_category="none")
        self.invalid(receipt)  # No fixture stage ever started.
        receipt = self.receipt()
        receipt["exact_cleanup"] = "not_run"
        self.invalid(receipt)  # Summary must match the cleanup check.

    def test_developer_mac_overrides_and_incomplete_environment_are_not_fresh_absence(self):
        mutations = (
            lambda r: r.update(tcc_precondition="granted", tcc_result="granted_without_prompt"),
            lambda r: r.update(xcode="installed"),
            lambda r: r.update(command_line_tools="installed"),
            lambda r: r.update(command_line_tools="not_checked"),
            lambda r: r["python"].update(source="homebrew"),
            lambda r: r["native_test_context"].update(subject="existing_user"),
            lambda r: r["native_test_context"].update(dependency_evidence="environment_override_only"),
            lambda r: r["native_test_context"].update(dependency_evidence="not_checked"),
            lambda r: r["native_test_context"].update(source_build="enabled"),
            lambda r: r["native_test_context"].update(source_build="not_checked"),
            lambda r: r.pop("native_test_context"),
        )
        for mutate in mutations:
            receipt = self.receipt()
            mutate(receipt)
            self.invalid(receipt)

    def test_new_scenario_requires_versioned_release_at_least_071(self):
        for ref in ("v0.7.0", "v0.6.1", "v00.7.1", "v0.7.1-rc1", "v0.7.1+local", "a" * 40):
            receipt = self.receipt()
            receipt["plugin_ref"] = ref
            self.invalid(receipt)
        for ref in ("v0.7.1", "v0.7.2", "v1.0.0"):
            receipt = self.receipt()
            receipt["plugin_ref"] = ref
            self.valid(receipt)

    def test_missing_duplicate_and_irrelevant_checks_are_rejected(self):
        for name in validator.FRESH_NATIVE_CHECKS:
            receipt = self.receipt()
            receipt["checks"] = [check for check in receipt["checks"] if check["id"] != name]
            self.invalid(receipt)
        for added in ({"id": "upgrade_identity", "outcome": "passed", "error_category": "none"},
                      {"id": "install", "outcome": "failed", "error_category": "unexpected_failure"}):
            receipt = self.receipt()
            receipt["checks"].append(added)
            self.invalid(receipt)

    def test_native_context_cannot_relabel_an_older_scenario(self):
        receipt = json.loads((ROOT / "docs/launch/examples/external-tester-receipt.example.json").read_text())
        self.valid(receipt)
        receipt["native_test_context"] = self.receipt()["native_test_context"]
        self.invalid(receipt)

    def test_cli_rejects_private_context_without_echoing_it(self):
        receipt = self.receipt()
        receipt["native_test_context"]["local_path"] = "/Users/private/secret.png"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt))
            completed = subprocess.run([sys.executable, str(ROOT / "scripts/validate_external_tester_receipt.py"), str(path)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, "receipt 1: invalid (privacy_error)\n")
        self.assertNotIn("private", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
