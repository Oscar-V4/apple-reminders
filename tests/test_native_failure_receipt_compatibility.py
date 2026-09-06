from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/apple-reminders"))
sys.path.insert(0, str(ROOT / "plugins/apple-reminders/scripts"))
import reminders_adapter as adapter
from mcp import server
from mcp.v2_native import FacadeError, NativeFacade
from mcp.v2_native_backend import NativeBackend
from mcp.v2_contract import validate_public_result
from mcp.v2_transport import DispatchCertainty, TransportResult
from reminders_service import Guard, MutationOutcome
from receipt_contract import adapter_receipt_error, validated_receipt_mutation_state


class NativeFailureReceiptCompatibilityTests(unittest.TestCase):
    def args(self):
        return argparse.Namespace(command="attach_image", id="SYNTHETIC-REMINDER",
            list_id=None, section_id=None, attachment_id=None)

    def failure(self, status="failed_manual_repair_required"):
        return adapter.command_failure_receipt(self.args(), "Private revision changed before confirmation.",
            code="concurrent_modification", status=status,
            expected_version=7, current_version=8)

    def public(self, receipt):
        original = deepcopy(receipt)
        references = Mock()
        references.revalidate_reference.return_value = Guard("SYNTHETIC-REMINDER", "SYNTHETIC-STORE", "revision")
        transport = Mock(return_value=TransportResult(payload=receipt, is_error=True, dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED))
        forbidden = Mock(side_effect=AssertionError("No extra backend or native operation"))
        backend = NativeBackend(bridge_call=forbidden, adapter_call=transport,
            build_adapter_argv=server.build_adapter_argv, receipt_validator=server.validate_adapter_receipt)
        facade = NativeFacade(adapter_call=forbidden, section_mutation=forbidden,
            references=references, native_read=forbidden, native_mutation=backend.mutate)
        with patch.object(backend, "_revalidate_guard", return_value={}), \
             patch.object(backend, "_private_attachments", return_value={"reminder_version": 7}), \
             patch.object(adapter, "run_bounded_process", side_effect=AssertionError("process launched")), \
             patch.object(adapter.sqlite3, "connect", side_effect=AssertionError("store opened")):
            result, state = facade.call_with_state("change_reminder_attachment", {
                "reference": "rev1." + "r" * 32,
                "action": {"kind": "attach_image", "image_path": "/synthetic/image.png", "idempotency_key": "synthetic-image-key"},
            })
        validate_public_result("change_reminder_attachment", result, state)
        transport.assert_called_once()
        references.read_exact.assert_not_called()
        self.assertEqual(receipt, original)
        return result, state

    def test_real_constructor_preserves_unknown_concurrency_through_public_facade(self):
        receipt = self.failure()
        self.assertEqual(receipt["before"], {})
        self.assertIsNone(adapter_receipt_error(receipt, expected_operation="attach_image"))
        result, state = self.public(receipt)
        self.assertEqual(state, "unknown")
        self.assertEqual(result["status"], "failed_manual_repair_required")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "concurrent_modification")
        self.assertEqual(result["error"]["reason_code"], "concurrent_modification")
        self.assertIsNone(result["verification"]["write_performed"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])
        summary = json.loads(server.tool_result(result, is_error=True)["content"][0]["text"])
        self.assertTrue(summary["may_have_mutated"])
        self.assertFalse(summary["next_read_only_action"]["retry_original_once"])
        self.assertNotIn("expected_version", json.dumps(result))
        self.assertNotIn("current_version", json.dumps(result))

    def test_affirmative_no_write_failure_is_not_upgraded_to_pending(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                receipt = self.failure("failed_no_mutation")
                if legacy:
                    receipt.pop("before")
                result, state = self.public(receipt)
                self.assertEqual(state, "not_mutated")
                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertIs(result["verification"]["write_performed"], False)
                self.assertEqual(result["error"]["code"], "concurrent_modification")

    def test_preexisting_durable_failure_replays_without_retry_or_error_masking(self):
        legacy = self.failure()
        legacy.pop("before")
        with tempfile.TemporaryDirectory() as directory:
            arguments = dict(args=self.args(), operation="attach_image", key="legacy-failure-key",
                             input_payload={"id": "SYNTHETIC-REMINDER"}, storage_dir=Path(directory))
            adapter.execute_idempotent_adapter_command(**arguments, callback=lambda: legacy)
            replay = adapter.execute_idempotent_adapter_command(**arguments,
                callback=lambda: self.fail("Unresolved cached failure must not retry"))
        self.assertNotIn("before", replay)
        self.assertNotIn("message", replay["error"])  # Durable privacy projection.
        result, state = self.public(replay)
        self.assertEqual(state, "unknown")
        self.assertEqual(result["status"], "failed_manual_repair_required")
        self.assertEqual(result["error"]["reason_code"], "concurrent_modification")
        self.assertTrue(result["replayed"])
        self.assertFalse(result["recovery"]["automatic_retry_safe"])

    def test_sensitive_failure_message_does_not_escape_public_projection(self):
        for legacy in (False, True):
            receipt = self.failure()
            receipt["error"].update(message="SECRET exception at /Users/private/secret-store.sqlite", reason_code="private_revision_changed")
            if legacy:
                receipt.pop("before")
            result, state = self.public(receipt)
            self.assertEqual(state, "unknown")
            self.assertEqual(result["error"]["reason_code"], "private_revision_changed")
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertNotIn("/Users/private", json.dumps(result))
            self.assertFalse(result["recovery"]["automatic_retry_safe"])

    def test_present_malformed_before_is_never_repaired(self):
        for status in ("failed_manual_repair_required", "failed_no_mutation"):
            state = "unknown" if status == "failed_manual_repair_required" else "not_mutated"
            for value in (None, [], "invalid", 1):
                with self.subTest(status=status, value=value):
                    receipt = self.failure(status)
                    receipt["before"] = value
                    with self.assertRaisesRegex(FacadeError, "objects are missing or invalid"):
                        NativeFacade._validated_outcome(MutationOutcome(receipt, state))
                    result, projected_state = self.public(receipt)
                    self.assertEqual(projected_state, "unknown")
                    self.assertEqual(result["status"], "committed_verification_pending")
                    self.assertEqual(result["error"]["reason_code"], "invalid_native_receipt_objects")

    def test_malformed_warnings_are_rejected_before_defaults(self):
        for value in (None, {}, [None], [1], [""]):
            with self.subTest(value=value):
                receipt = self.failure()
                receipt.pop("before")
                receipt["warnings"] = value
                with self.assertRaisesRegex(FacadeError, "warnings are invalid"):
                    NativeFacade._validated_outcome(MutationOutcome(receipt, "unknown"))
                result, state = self.public(receipt)
                self.assertEqual(state, "unknown")
                self.assertEqual(result["error"]["reason_code"], "invalid_native_receipt_warnings")

    def test_missing_before_does_not_relax_success_or_contradictory_no_write_evidence(self):
        receipt = self.failure()
        receipt.pop("before")
        receipt.update(status="verified", ok=True, after={"synthetic": True})
        with self.assertRaisesRegex(FacadeError, "objects are missing or invalid"):
            NativeFacade._validated_outcome(MutationOutcome(receipt, "committed"))
        receipt = self.failure("failed_no_mutation")
        receipt.pop("before")
        receipt["verification"]["write_performed"] = True
        self.assertEqual(validated_receipt_mutation_state(receipt), "unknown")
        result, state = self.public(receipt)
        self.assertEqual(state, "unknown")
        self.assertNotEqual(result["status"], "failed_no_mutation")
        self.assertFalse(result["recovery"]["automatic_retry_safe"])


if __name__ == "__main__":
    unittest.main()
