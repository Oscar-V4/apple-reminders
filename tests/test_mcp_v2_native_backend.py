from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_native_backend import NativeBackend
from reminders_service import Guard


REMINDER_ID = "REMINDER-EXACT-1"
GUARD = Guard(
    reminder_id=REMINDER_ID,
    store_identity="eventkit:SOURCE-1",
    public_concurrency_value="2026-08-25T00:00:00Z",
)
SOURCE_REMINDER_ID = "REMINDER-SOURCE-1"
SOURCE_GUARD = Guard(
    reminder_id=SOURCE_REMINDER_ID,
    store_identity="eventkit:SOURCE-2",
    public_concurrency_value="2026-08-25T00:00:01Z",
)


def verified_public_reminder() -> tuple[dict[str, Any], bool]:
    return (
        {
            "ok": True,
            "status": "verified",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "source_id": "SOURCE-1",
                    "last_modified": "2026-08-25T00:00:00Z",
                }
            },
        },
        False,
    )


class NativeBackendInterfaceTests(unittest.TestCase):
    def make_backend(
        self,
        *,
        bridge_call: Any,
        adapter_call: Any,
        argv_calls: list[tuple[str, dict[str, Any]]],
        receipt_error: str | None = None,
    ) -> NativeBackend:
        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        return NativeBackend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
            receipt_validator=mock.Mock(return_value=receipt_error),
        )

    def test_mutation_revalidates_then_uses_fresh_exact_private_revision(self) -> None:
        trace: list[str] = []

        def bridge_call(operation: str, arguments: dict[str, Any]) -> Any:
            trace.append(f"bridge:{operation}")
            return verified_public_reminder()

        receipt = {
            "ok": True,
            "status": "verified",
            "operation": "attach_url",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "sqlite_private",
            "target": {"reminder_id": REMINDER_ID},
            "before": {},
            "after": {},
            "verification": {"state": "read_back"},
            "recovery": {"semantics": "delete_attachment"},
        }
        adapter_replies = iter(
            [
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                    },
                    False,
                ),
                (receipt, False),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 13,
                        "attachments": [
                            {
                                "id": "ATTACHMENT-1",
                                "type": "url",
                                "url": "https://example.com/item",
                            }
                        ],
                    },
                    False,
                ),
            ]
        )

        def adapter_call(argv: list[str]) -> Any:
            trace.append(f"adapter:{argv[0]}")
            return next(adapter_replies)

        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = self.make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            argv_calls=argv_calls,
        )

        outcome = backend.mutate(
            GUARD,
            "attach_url",
            {"url": "https://example.com/item"},
        )

        self.assertEqual(outcome.mutation_state, "committed")
        self.assertEqual(
            trace,
            [
                "bridge:read_reminder",
                "adapter:list_reminder_attachments",
                "adapter:attach_url_to_reminder",
                "adapter:list_reminder_attachments",
            ],
        )
        self.assertEqual(
            argv_calls[1],
            (
                "attach_url_to_reminder",
                {
                    "reminder_id": REMINDER_ID,
                    "url": "https://example.com/item",
                    "if_version": 12,
                },
            ),
        )
        self.assertEqual(outcome.receipt["after"]["reminder"]["id"], REMINDER_ID)

    def test_private_reads_reject_missing_or_conflicting_exact_identity(self) -> None:
        for payload in (
            {"ok": True, "reminder": {"id": "OTHER"}},
            {"ok": True, "reminder": {"title": "Identity omitted"}},
        ):
            with self.subTest(payload=payload):
                backend = self.make_backend(
                    bridge_call=mock.Mock(return_value=verified_public_reminder()),
                    adapter_call=mock.Mock(return_value=(payload, False)),
                    argv_calls=[],
                )
                with self.assertRaises(RuntimeError):
                    backend.read(GUARD, {"include": ["section"]})

        for payload in (
            {"ok": True, "attachments": [], "reminder_version": 1},
            {
                "ok": True,
                "reminder_id": "OTHER",
                "attachments": [],
                "reminder_version": 1,
            },
        ):
            with self.subTest(payload=payload):
                backend = self.make_backend(
                    bridge_call=mock.Mock(return_value=verified_public_reminder()),
                    adapter_call=mock.Mock(return_value=(payload, False)),
                    argv_calls=[],
                )
                with self.assertRaises(RuntimeError):
                    backend.read(GUARD, {"include": ["attachments"], "limit": 1})

    def test_failed_manual_receipt_is_not_downgraded_to_pending(self) -> None:
        receipt = {
            "ok": False,
            "status": "failed_manual_repair_required",
            "operation": "attach_url",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "sqlite_private",
            "target": {"reminder_id": REMINDER_ID},
            "before": {},
            "after": {},
            "verification": {"state": "partial", "write_performed": True},
            "recovery": {
                "semantics": "manual_repair_required",
                "automatic_retry_safe": False,
            },
            "error": {
                "code": "unexpected_error",
                "message": "Compensation did not complete.",
            },
        }
        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                    },
                    False,
                ),
                (receipt, True),
            ]
        )
        backend = self.make_backend(
            bridge_call=mock.Mock(return_value=verified_public_reminder()),
            adapter_call=adapter_call,
            argv_calls=[],
        )

        outcome = backend.mutate(
            GUARD,
            "attach_url",
            {"url": "https://example.com/item"},
        )

        self.assertEqual(outcome.receipt["status"], "failed_manual_repair_required")
        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(adapter_call.call_count, 2)

    def test_public_adapter_read_preserves_exact_account_scope(self) -> None:
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        adapter_call = mock.Mock(return_value=({"ok": True, "tags": []}, False))
        backend = self.make_backend(
            bridge_call=mock.Mock(),
            adapter_call=adapter_call,
            argv_calls=argv_calls,
        )

        result = backend.adapter_call(
            "list_tags",
            {"account_id": "ACCOUNT-1", "query": "next", "limit": 25},
        )

        self.assertEqual(result, {"ok": True, "tags": []})
        self.assertEqual(
            argv_calls,
            [
                (
                    "list_reminder_tags",
                    {"account_id": "ACCOUNT-1", "query": "next", "limit": 25},
                )
            ],
        )

    def test_copy_image_rechecks_two_guards_and_routes_only_exact_private_ids(
        self,
    ) -> None:
        trace: list[str] = []

        def bridge_call(operation: str, arguments: dict[str, Any]) -> Any:
            reminder_id = arguments["reminder_id"]
            trace.append(f"bridge:{reminder_id}")
            guard = SOURCE_GUARD if reminder_id == SOURCE_REMINDER_ID else GUARD
            source_id = guard.store_identity.removeprefix("eventkit:")
            return (
                {
                    "ok": True,
                    "status": "verified",
                    "data": {
                        "reminder": {
                            "id": reminder_id,
                            "source_id": source_id,
                            "last_modified": guard.public_concurrency_value,
                        }
                    },
                },
                False,
            )

        source_attachment = {
            "id": "ATTACHMENT-SOURCE",
            "type": "image",
            "uti": "public.png",
            "filename": "source.png",
            "sha512": "a" * 128,
            "file_size": 100,
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        destination_attachment = {
            **source_attachment,
            "id": "ATTACHMENT-NEW",
            "filename": "copied.png",
            "uti": "public.jpeg",
        }
        receipt = {
            "ok": True,
            "status": "verified",
            "operation": "copy_image",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "reminderkit_private",
            "target": {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "reminder_id": REMINDER_ID,
                "source_attachment_id": "ATTACHMENT-SOURCE",
                "attachment_id": "ATTACHMENT-NEW",
            },
            "before": {"reminder": {"id": REMINDER_ID}},
            "after": {
                "reminder": {"id": REMINDER_ID},
                "attachment": destination_attachment,
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "delete_copied_attachment_with_fresh_reference",
                "automatic_retry_safe": False,
            },
        }
        adapter_replies = iter(
            [
                (
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    },
                    False,
                ),
                (receipt, False),
                (
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 13,
                        "attachments": [destination_attachment],
                        "truncated": False,
                    },
                    False,
                ),
            ]
        )

        def adapter_call(argv: list[str]) -> Any:
            trace.append(f"adapter:{argv[0]}")
            return next(adapter_replies)

        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = self.make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            argv_calls=argv_calls,
        )

        outcome = backend.copy_image(
            GUARD,
            SOURCE_GUARD,
            "copy_image",
            {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "attachment_id": "ATTACHMENT-SOURCE",
                "idempotency_key": "copy-image-backend",
            },
        )

        self.assertEqual(outcome.mutation_state, "committed")
        self.assertEqual(
            trace,
            [
                f"bridge:{REMINDER_ID}",
                f"bridge:{SOURCE_REMINDER_ID}",
                "adapter:list_reminder_attachments",
                "adapter:list_reminder_attachments",
                "adapter:copy_image_attachment",
                "adapter:list_reminder_attachments",
                "adapter:list_reminder_attachments",
            ],
        )
        self.assertEqual(
            argv_calls[2],
            (
                "copy_image_attachment",
                {
                    "source_reminder_id": SOURCE_REMINDER_ID,
                    "reminder_id": REMINDER_ID,
                    "attachment_id": "ATTACHMENT-SOURCE",
                    "if_source_version": 7,
                    "if_version": 12,
                    "idempotency_key": "copy-image-backend",
                },
            ),
        )
        self.assertNotIn("image_path", argv_calls[2][1])
        self.assertTrue(outcome.receipt["verification"]["source_unchanged"])
        self.assertEqual(
            outcome.receipt["target"]["attachment_id"], "ATTACHMENT-NEW"
        )

    def test_copy_image_missing_exact_source_attachment_never_dispatches(self) -> None:
        def bridge_call(operation: str, arguments: dict[str, Any]) -> Any:
            guard = (
                SOURCE_GUARD
                if arguments["reminder_id"] == SOURCE_REMINDER_ID
                else GUARD
            )
            return (
                {
                    "ok": True,
                    "status": "verified",
                    "data": {
                        "reminder": {
                            "id": guard.reminder_id,
                            "source_id": guard.store_identity.removeprefix("eventkit:"),
                            "last_modified": guard.public_concurrency_value,
                        }
                    },
                },
                False,
            )

        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    },
                    False,
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = self.make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            argv_calls=argv_calls,
        )

        outcome = backend.copy_image(
            GUARD,
            SOURCE_GUARD,
            "copy_image",
            {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "attachment_id": "ATTACHMENT-MISSING",
                "idempotency_key": "copy-image-missing",
            },
        )

        self.assertEqual(outcome.mutation_state, "not_mutated")
        self.assertEqual(outcome.receipt["status"], "failed_no_mutation")
        self.assertFalse(
            any(name == "copy_image_attachment" for name, _ in argv_calls)
        )

    def test_copy_image_adapter_preflight_failure_remains_proven_no_write(self) -> None:
        def bridge_call(operation: str, arguments: dict[str, Any]) -> Any:
            guard = (
                SOURCE_GUARD
                if arguments["reminder_id"] == SOURCE_REMINDER_ID
                else GUARD
            )
            return (
                {
                    "ok": True,
                    "status": "verified",
                    "data": {
                        "reminder": {
                            "id": guard.reminder_id,
                            "source_id": guard.store_identity.removeprefix("eventkit:"),
                            "last_modified": guard.public_concurrency_value,
                        }
                    },
                },
                False,
            )

        source_attachment = {
            "id": "ATTACHMENT-SOURCE",
            "type": "image",
            "uti": "public.png",
            "filename": "source.png",
            "sha512": "a" * 128,
            "file_size": 100,
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        adapter_call = mock.Mock(
            side_effect=[
                (
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    },
                    False,
                ),
                (
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "copy_image_attachment",
                        "operation_id": "33333333-3333-4333-8333-333333333333",
                        "backend": "adapter",
                        "target": {},
                        "before": {},
                        "after": {},
                        "verification": {
                            "state": "not_needed",
                            "write_performed": False,
                            "final_read": False,
                        },
                        "recovery": {
                            "semantics": "retry_after_fixing_input",
                            "automatic_retry_safe": False,
                        },
                        "error": {
                            "code": "ambiguous_target",
                            "reason_code": "source_image_files_diverge",
                            "message": "Source files diverged.",
                            "retryable": False,
                        },
                    },
                    True,
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        outcome = self.make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            argv_calls=argv_calls,
        ).copy_image(
            GUARD,
            SOURCE_GUARD,
            "copy_image",
            {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "attachment_id": "ATTACHMENT-SOURCE",
                "idempotency_key": "copy-image-preflight-failure",
            },
        )

        self.assertEqual(outcome.mutation_state, "not_mutated")
        self.assertEqual(outcome.receipt["status"], "failed_no_mutation")
        self.assertEqual(outcome.receipt["operation"], "copy_image")
        self.assertFalse(outcome.receipt["verification"]["write_performed"])

    def test_copy_image_digest_mismatch_is_pending_and_never_terminal_verified(
        self,
    ) -> None:
        source_attachment = {
            "id": "ATTACHMENT-SOURCE",
            "type": "image",
            "uti": "public.png",
            "sha512": "a" * 128,
            "file_size": 100,
            "width": 10,
            "height": 10,
            "marked_for_deletion": False,
        }
        destination_attachment = {
            **source_attachment,
            "id": "ATTACHMENT-NEW",
            "sha512": "b" * 128,
        }
        receipt = {
            "ok": True,
            "status": "verified",
            "operation": "copy_image",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "reminderkit_private",
            "target": {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "reminder_id": REMINDER_ID,
                "source_attachment_id": "ATTACHMENT-SOURCE",
                "attachment_id": "ATTACHMENT-NEW",
            },
            "before": {"reminder": {"id": REMINDER_ID}},
            "after": {"attachment": destination_attachment},
            "verification": {"state": "read_back", "write_performed": True},
            "recovery": {"semantics": "delete_copied_attachment"},
        }
        adapter_call = mock.Mock(return_value=(receipt, False))
        backend = self.make_backend(
            bridge_call=mock.Mock(),
            adapter_call=adapter_call,
            argv_calls=[],
        )
        backend._revalidate_guard = mock.Mock()  # type: ignore[method-assign]
        backend._private_attachments = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "reminder_id": SOURCE_REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [source_attachment],
                    "truncated": False,
                },
                {
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 12,
                    "attachments": [],
                    "truncated": False,
                },
                {
                    "reminder_id": SOURCE_REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [source_attachment],
                    "truncated": False,
                },
                {
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 13,
                    "attachments": [destination_attachment],
                    "truncated": False,
                },
            ]
        )

        outcome = backend.copy_image(
            GUARD,
            SOURCE_GUARD,
            "copy_image",
            {
                "source_reminder_id": SOURCE_REMINDER_ID,
                "attachment_id": "ATTACHMENT-SOURCE",
                "idempotency_key": "copy-image-digest-mismatch",
            },
        )

        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(outcome.receipt["status"], "committed_verification_pending")
        self.assertFalse(outcome.receipt["verification"]["final_read"])


if __name__ == "__main__":
    unittest.main()
