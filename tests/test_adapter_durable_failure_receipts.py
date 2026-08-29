from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PATH = (
    REPO_ROOT
    / "plugins"
    / "apple-reminders"
    / "scripts"
    / "reminders_adapter.py"
)
SPEC = importlib.util.spec_from_file_location(
    "reminders_adapter_durable_failure_receipts",
    ADAPTER_PATH,
)
assert SPEC and SPEC.loader
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


REMINDER_ID = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"


class AdapterDurableFailureReceiptTests(unittest.TestCase):
    def args(self, command: str = "attach_image") -> argparse.Namespace:
        return argparse.Namespace(
            command=command,
            id=REMINDER_ID,
            list_id=None,
            section_id=None,
            attachment_id=None,
        )

    def execute(
        self,
        storage_dir: Path,
        callback,
        *,
        command: str = "attach_image",
        key: str | None = "stable-key",
    ) -> dict:
        return adapter.execute_idempotent_adapter_command(
            args=self.args(command),
            operation=command,
            key=key,
            input_payload={"id": REMINDER_ID},
            callback=callback,
            storage_dir=storage_dir,
        )

    def test_unclassified_adapter_error_is_persisted_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)
            calls = 0

            def fail_once() -> dict:
                nonlocal calls
                calls += 1
                raise adapter.AdapterError(
                    "The exact target no longer exists.",
                    code="not_found",
                    reason_code="reminder_missing",
                )

            first = self.execute(storage_dir, fail_once)
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(calls, 1)
            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertEqual(first["operation"], "attach_image")
            self.assertIsNone(first["verification"]["write_performed"])
            self.assertEqual(first["error"]["code"], "not_found")
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])
            self.assertTrue(replay["replayed"])

            store = json.loads(
                (storage_dir / "idempotency.json").read_text(encoding="utf-8")
            )
            record = next(iter(store["entries"].values()))
            self.assertEqual(record["state"], "complete")
            self.assertEqual(
                record["result"]["status"],
                "failed_manual_repair_required",
            )

    def test_partial_adapter_failure_is_persisted_as_manual_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            def fail_after_possible_write() -> dict:
                raise adapter.AdapterError(
                    "The mutation and compensation both need inspection.",
                    code="sync_pending",
                    partial_failure=True,
                    compensated=False,
                    compensation_error="cleanup failed",
                )

            first = self.execute(storage_dir, fail_after_possible_write)
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertIsNone(first["verification"]["write_performed"])
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])
            self.assertTrue(replay["replayed"])

    def test_untyped_compensation_claim_is_persisted_conservatively(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            first = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(
                    adapter.AdapterError(
                        "The attempted change was compensated.",
                        code="sync_pending",
                        partial_failure=True,
                        compensated=True,
                    )
                ),
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertIsNone(first["verification"]["write_performed"])
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])

    def test_untyped_no_write_status_cannot_bypass_the_typed_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            first = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(
                    adapter.AdapterError(
                        "Untyped no-write claim.",
                        code="sync_pending",
                        result_status="failed_no_mutation",
                    )
                ),
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertEqual(
                first["error"]["reason_code"],
                "untyped_adapter_result_status_ignored",
            )
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])

    def test_unexpected_exception_is_persisted_as_manual_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            first = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(RuntimeError("synthetic crash")),
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertEqual(first["error"]["code"], "unexpected_error")
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])
            self.assertTrue(replay["replayed"])

    def test_invalid_explicit_status_becomes_a_replayable_manual_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            first = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(
                    adapter.AdapterError(
                        "Invalid internal classification.",
                        code="unexpected_error",
                        result_status={"bogus": True},
                    )
                ),
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertEqual(
                first["error"]["reason_code"],
                "invalid_adapter_result_status",
            )
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])

    def test_non_json_error_detail_cannot_leave_an_unresolved_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            first = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(
                    adapter.AdapterError(
                        "Unknown post-dispatch failure.",
                        code="sync_pending",
                        partial_failure=True,
                        opaque_id=object(),
                    )
                ),
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
            )

            self.assertEqual(first["status"], "failed_manual_repair_required")
            self.assertEqual(replay["status"], first["status"])
            self.assertEqual(replay["operation_id"], first["operation_id"])

    def test_typed_not_started_error_clears_fence_and_allows_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)

            with self.assertRaises(adapter.MutationNotStartedError):
                self.execute(
                    storage_dir,
                    lambda: (_ for _ in ()).throw(
                        adapter.MutationNotStartedError(
                            "Dispatch never started.",
                            code="permission_denied",
                        )
                    ),
                )

            success = self.execute(
                storage_dir,
                lambda: adapter.operation_receipt(
                    status="verified",
                    operation="attach_image",
                    backend="synthetic",
                    target={"id": REMINDER_ID},
                    after={},
                    verification={
                        "state": "read_back",
                        "write_performed": True,
                        "final_read": True,
                    },
                    recovery={"semantics": "not_applicable"},
                ),
            )

            self.assertEqual(success["status"], "verified")
            store = json.loads(
                (storage_dir / "idempotency.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(store["entries"]), 1)
            self.assertEqual(next(iter(store["entries"].values()))["state"], "complete")

    def test_unkeyed_callback_keeps_the_existing_outer_error_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(adapter.AdapterError):
                self.execute(
                    Path(temp_dir),
                    lambda: (_ for _ in ()).throw(
                        adapter.AdapterError("Invalid input.", code="invalid_input")
                    ),
                    key=None,
                )

    def test_explicit_command_mismatch_fails_before_creating_a_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)
            with self.assertRaises(ValueError):
                adapter.execute_idempotent_adapter_command(
                    args=self.args("replace_attachment"),
                    operation="attach_image",
                    key="mismatched-command",
                    input_payload={"id": REMINDER_ID},
                    callback=lambda: self.fail("The callback must not run."),
                    storage_dir=storage_dir,
                )

            self.assertFalse((storage_dir / "idempotency.json").exists())

    def test_copy_failure_uses_the_public_receipt_operation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            storage_dir = Path(temp_dir)
            failure = self.execute(
                storage_dir,
                lambda: (_ for _ in ()).throw(
                    adapter.AdapterError("Source missing.", code="not_found")
                ),
                command="copy_image_attachment",
            )
            replay = self.execute(
                storage_dir,
                lambda: self.fail("A replay must not dispatch the callback."),
                command="copy_image_attachment",
            )

            self.assertEqual(failure["operation"], "copy_image")
            self.assertEqual(failure["status"], "failed_manual_repair_required")
            self.assertEqual(replay["operation"], "copy_image")
            self.assertEqual(replay["operation_id"], failure["operation_id"])

    def test_attach_image_returns_failure_exit_code_from_persisted_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "image.png"
            image.write_bytes(b"synthetic-image")
            args = argparse.Namespace(
                command="attach_image",
                db=None,
                backend="reminderkit",
                id=REMINDER_ID,
                title=None,
                list=None,
                image=str(image),
                if_version=1,
                idempotency_key="attach-key",
            )
            failure = adapter.command_failure_receipt(
                args,
                "Synthetic failure.",
                code="not_found",
                status="failed_no_mutation",
            )

            with (
                mock.patch.object(
                    adapter,
                    "execute_idempotent_adapter_command",
                    return_value=failure,
                ) as execute,
                mock.patch.object(adapter, "json_out") as json_out,
            ):
                exit_code = adapter.cmd_attach_image(args)

            self.assertEqual(exit_code, 1)
            self.assertEqual(execute.call_args.kwargs["operation"], "attach_image")
            self.assertEqual(execute.call_args.kwargs["key"], "attach-key")
            json_out.assert_called_once_with(failure)


if __name__ == "__main__":
    unittest.main()
