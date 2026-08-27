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
from mcp.v2_transport import DispatchCertainty, TransportResult
from reminders_service import Guard
from receipt_contract import adapter_receipt_error


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


def transport(payload: dict[str, Any], *, is_error: bool = False) -> TransportResult:
    return TransportResult(
        payload,
        is_error,
        DispatchCertainty.MAY_HAVE_STARTED,
    )


def verified_public_reminder() -> TransportResult:
    return transport(
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
        }
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
            "target": {
                "reminder_id": REMINDER_ID,
                "attachment_id": "ATTACHMENT-1",
            },
            "before": {},
            "after": {},
            "verification": {"state": "read_back"},
            "recovery": {"semantics": "delete_attachment"},
        }
        adapter_replies = iter(
            [
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                    }
                ),
                transport(receipt),
                transport(
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
                    }
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

    def test_terminal_mutation_is_pending_when_private_final_state_misses_action(self) -> None:
        receipt = {
            "ok": True,
            "status": "verified",
            "operation": "attach_url",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "sqlite_private",
            "target": {"reminder_id": REMINDER_ID, "attachment_id": "ATTACHMENT-1"},
            "before": {},
            "after": {"attachment": {"id": "ATTACHMENT-1", "type": "url", "url": "https://example.com/item"}},
            "verification": {"state": "read_back", "write_performed": True},
            "recovery": {"semantics": "delete_attachment"},
        }
        adapter_replies = iter(
            [
                transport({"ok": True, "reminder_id": REMINDER_ID, "reminder_version": 12, "attachments": []}),
                transport(receipt),
                transport({"ok": True, "reminder_id": REMINDER_ID, "reminder_version": 13, "attachments": []}),
            ]
        )
        backend = self.make_backend(
            bridge_call=mock.Mock(return_value=verified_public_reminder()),
            adapter_call=mock.Mock(side_effect=lambda _argv: next(adapter_replies)),
            argv_calls=[],
        )

        outcome = backend.mutate(
            GUARD,
            "attach_url",
            {"url": "https://example.com/item"},
        )

        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(outcome.receipt["status"], "committed_verification_pending")
        self.assertEqual(outcome.receipt["error"]["reason_code"], "native_final_state_mismatch")
        self.assertFalse(outcome.receipt["recovery"]["automatic_retry_safe"])

    def test_final_state_matching_canonicalizes_native_identifiers_and_tags(self) -> None:
        old_upper = "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"
        old_lower = old_upper.lower()
        new_upper = "11111111-2222-4333-8444-555555555555"
        existing = {
            "attachments": [{"id": old_upper, "type": "image"}],
            "truncated": False,
        }
        replaced_but_not_deleted = {
            "attachments": [
                {"id": old_upper, "type": "image"},
                {"id": new_upper, "type": "image"},
            ],
            "truncated": False,
        }

        self.assertFalse(
            NativeBackend._final_state_matches(
                "delete_attachment", {"attachment_id": old_lower}, {}, existing
            )
        )
        self.assertFalse(
            NativeBackend._final_state_matches(
                "replace_image",
                {"attachment_id": old_lower},
                {"target": {"new_attachment_id": new_upper.lower()}},
                replaced_but_not_deleted,
            )
        )
        self.assertTrue(
            NativeBackend._final_state_matches(
                "move_to_section",
                {"section_id": old_lower},
                {},
                {"section_id": old_upper},
            )
        )
        self.assertTrue(
            NativeBackend._final_state_matches(
                "add_tag", {"tag": "#Next"}, {}, {"tags": [{"name": "next"}]}
            )
        )
        self.assertFalse(
            NativeBackend._final_state_matches(
                "remove_tag", {"tag": "next"}, {}, {"id": REMINDER_ID}
            )
        )
        self.assertFalse(
            NativeBackend._final_state_matches(
                "remove_tag", {"tag": "next"}, {}, {"tags": [{}]}
            )
        )
        self.assertFalse(
            NativeBackend._final_state_matches(
                "delete_attachment",
                {"attachment_id": old_lower},
                {},
                {"attachments": [{"type": "image"}], "truncated": False},
            )
        )
        self.assertFalse(
            NativeBackend._final_state_matches(
                "delete_attachment",
                {"attachment_id": old_lower},
                {},
                {"attachments": [], "truncated": True},
            )
        )
        self.assertTrue(
            NativeBackend._final_state_matches(
                "delete_attachment",
                {"attachment_id": old_lower},
                {},
                {"attachments": [], "truncated": False},
            )
        )

    def test_image_final_state_binds_snapshot_content_and_mobile_visibility(self) -> None:
        expected = {
            "id": "ATTACHMENT-NEW",
            "type": "image",
            "uti": "public.png",
            "sha512": "a" * 128,
            "file_size": 100,
            "width": 10,
            "height": 20,
        }
        payload = {"target": {"attachment_id": "ATTACHMENT-NEW"}}
        adapter_after = {"attachment": copy.deepcopy(expected)}
        final_attachment = {
            **expected,
            "sync": {"mobile_visible_likely": False},
        }

        self.assertFalse(
            NativeBackend._final_state_matches(
                "attach_image",
                {},
                payload,
                {"attachments": [final_attachment], "truncated": False},
                adapter_after=adapter_after,
            )
        )
        final_attachment["sync"]["mobile_visible_likely"] = True
        final_attachment["sha512"] = "b" * 128
        self.assertFalse(
            NativeBackend._final_state_matches(
                "attach_image",
                {},
                payload,
                {"attachments": [final_attachment], "truncated": False},
                adapter_after=adapter_after,
            )
        )
        final_attachment["sha512"] = "a" * 128
        self.assertTrue(
            NativeBackend._final_state_matches(
                "attach_image",
                {},
                payload,
                {"attachments": [final_attachment], "truncated": False},
                adapter_after=adapter_after,
            )
        )
        replay_payload = {
            "target": {"attachment_id": "ATTACHMENT-NEW"},
            "replayed": True,
            "verification": {
                "final_attachment_content_hash": NativeBackend._image_content_hash(
                    expected
                )
            },
        }
        self.assertTrue(
            NativeBackend._final_state_matches(
                "attach_image",
                {},
                replay_payload,
                {"attachments": [final_attachment], "truncated": False},
                adapter_after={"attachment": {"id": "ATTACHMENT-NEW"}},
            )
        )

    def test_private_reads_reject_missing_or_conflicting_exact_identity(self) -> None:
        for payload in (
            {"ok": True, "reminder": {"id": "OTHER"}},
            {"ok": True, "reminder": {"title": "Identity omitted"}},
        ):
            with self.subTest(payload=payload):
                backend = self.make_backend(
                    bridge_call=mock.Mock(return_value=verified_public_reminder()),
                    adapter_call=mock.Mock(return_value=transport(payload)),
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
                    adapter_call=mock.Mock(return_value=transport(payload)),
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
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                    }
                ),
                transport(receipt, is_error=True),
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
        adapter_call = mock.Mock(return_value=transport({"ok": True, "tags": []}))
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

    def test_contradictory_failed_no_mutation_receipt_stays_unknown(self) -> None:
        contradictory = {
            "ok": False,
            "status": "failed_no_mutation",
            "operation": "attach_url",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "sqlite_private",
            "target": {"reminder_id": REMINDER_ID, "attachment_id": "A-1"},
            "before": {},
            "after": {"attachment": {"id": "A-1", "type": "url"}},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
            },
            "recovery": {"semantics": "not_applicable"},
            "error": {"code": "unexpected_error", "message": "Contradictory"},
        }
        error = adapter_receipt_error(
            contradictory,
            expected_operation="attach_url",
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                    }
                ),
                transport(contradictory, is_error=True),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = self.make_backend(
            bridge_call=mock.Mock(return_value=verified_public_reminder()),
            adapter_call=adapter_call,
            argv_calls=argv_calls,
            receipt_error=error,
        )

        outcome = backend.mutate(
            GUARD,
            "attach_url",
            {"url": "https://example.com"},
        )

        self.assertIsNotNone(error)
        self.assertEqual(outcome.mutation_state, "unknown")
        self.assertEqual(outcome.receipt["status"], "committed_verification_pending")
        self.assertIsNone(outcome.receipt["verification"]["write_performed"])
        self.assertEqual(
            outcome.receipt["error"]["reason_code"],
            "invalid_native_mutation_receipt",
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
            return transport(
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
                }
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
            "sync": {"mobile_visible_likely": True},
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
                transport(
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(receipt),
                transport(
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 13,
                        "attachments": [destination_attachment],
                        "truncated": False,
                    }
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
            return transport(
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
                }
            )

        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    }
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
            return transport(
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
                }
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
                transport(
                    {
                        "ok": True,
                        "reminder_id": SOURCE_REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [source_attachment],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 12,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
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
                    is_error=True,
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
        adapter_call = mock.Mock(return_value=transport(receipt))
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
