from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(PLUGIN_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

import durable_idempotency
from durable_idempotency import execute_idempotent
from mcp import server as mcp_server
from mcp.v2_core import V2CoreFacade
from mcp.v2_core_backend import CoreBackend
from mcp.v2_contract import validate_public_result
from mcp.v2_transport import DispatchCertainty, TransportResult


REMINDER_ID = "REMINDER-EXACT-1"


def transport(
    payload: dict[str, Any],
    *,
    is_error: bool | None = None,
    proves_not_started: bool = False,
) -> TransportResult:
    """Build the typed result returned by the local bridge/adapter launchers."""
    return TransportResult(
        payload=payload,
        is_error=payload.get("ok") is not True if is_error is None else is_error,
        dispatch_certainty=(
            DispatchCertainty.PROVEN_NOT_STARTED
            if proves_not_started
            else DispatchCertainty.MAY_HAVE_STARTED
        ),
    )


def valid_eventkit_receipt(
    operation: str,
    *,
    status: str = "verified",
    **overrides: Any,
) -> dict[str, Any]:
    """Build a complete EventKit mutation Receipt for Core transport tests."""
    write_performed = status == "verified"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "operation": operation,
        "status": status,
        "ok": True,
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "backend": "eventkit_public_sdk",
        "target": {"id": REMINDER_ID},
        "after": {"id": REMINDER_ID},
        "verification": {
            "state": "read_back",
            "write_performed": write_performed,
            "final_read": True,
            "matched": True,
        },
        "recovery": {
            "semantics": "not_applicable",
            "automatic_retry_safe": not write_performed,
        },
    }
    if operation != "create_reminder":
        payload["before"] = {"id": REMINDER_ID}
    payload.update(copy.deepcopy(overrides))
    return payload


def idempotency_passthrough(**arguments: Any) -> dict[str, Any]:
    return arguments["callback"]()


def bound_idempotency(storage_dir: Path) -> Any:
    return partial(execute_idempotent, storage_dir=storage_dir)


def without_durable_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.pop("idempotency_key_hash", None)
    result.pop("replayed", None)
    return result


def make_backend(
    *,
    bridge_call: Any,
    adapter_call: Any | None = None,
    build_adapter_argv: Any | None = None,
    receipt_validator: Any | None = None,
    idempotency_call: Any | None = None,
) -> CoreBackend:
    return CoreBackend(
        bridge_call=bridge_call,
        adapter_call=adapter_call or mock.Mock(),
        build_adapter_argv=build_adapter_argv or mock.Mock(),
        idempotency_call=idempotency_call or idempotency_passthrough,
        receipt_validator=receipt_validator or mock.Mock(return_value=None),
    )


def ensure_list_receipt(
    *,
    list_id: str,
    source_id: str,
    name: str,
    status: str,
    operation_id: str,
) -> dict[str, Any]:
    reminder_list = {
        "id": list_id,
        "title": name,
        "type": "caldav",
        "allows_content_modifications": True,
        "subscribed": False,
        "immutable": False,
        "source": {
            "id": source_id,
            "title": "Test Account",
            "type": "caldav",
            "is_delegate": False,
            "reminder_calendar_count": 3,
        },
    }
    return {
        "schema_version": 1,
        "ok": True,
        "status": status,
        "operation": "ensure_reminder_list",
        "operation_id": operation_id,
        "backend": "eventkit_public_sdk",
        "target": {"source_id": source_id, "list_id": list_id},
        "before": copy.deepcopy(reminder_list) if status == "unchanged" else {},
        "after": copy.deepcopy(reminder_list),
        "verification": {
            "state": "read_back",
            "write_performed": status == "verified",
            "final_read": True,
            "matched": True,
        },
        "recovery": {
            "semantics": "not_applicable",
            "automatic_retry_safe": status == "unchanged",
        },
    }


class RacingEnsureListBridge:
    """Deterministic fake for EventKit's non-atomic check-then-create flow."""

    def __init__(self) -> None:
        self._race = threading.Barrier(2)
        self._lock = threading.Lock()
        self._lists: list[dict[str, str]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def created_lists(self) -> list[dict[str, str]]:
        with self._lock:
            return copy.deepcopy(self._lists)

    def __call__(
        self,
        operation: str,
        arguments: dict[str, Any],
    ) -> TransportResult:
        if operation != "ensure_reminder_list":
            raise AssertionError(f"unexpected bridge operation: {operation}")
        source_id = str(arguments["source_id"])
        name = str(arguments["name"])
        with self._lock:
            self.calls.append((operation, copy.deepcopy(arguments)))
            matches = [
                copy.deepcopy(item)
                for item in self._lists
                if item["source_id"] == source_id and item["title"] == name
            ]

        # When callbacks overlap, both finish their empty read before either
        # creates. When a durable lock serializes them, the first wait times out
        # and the second callback observes the newly-created exact-name list.
        try:
            self._race.wait(timeout=0.15)
        except threading.BrokenBarrierError:
            pass

        if matches:
            item = matches[0]
            status = "unchanged"
        else:
            with self._lock:
                sequence = len(self._lists) + 1
                item = {
                    "id": f"LIST-{sequence}",
                    "source_id": source_id,
                    "title": name,
                }
                self._lists.append(item)
            status = "verified"
        sequence = int(item["id"].removeprefix("LIST-"))
        return transport(
            ensure_list_receipt(
                list_id=item["id"],
                source_id=source_id,
                name=name,
                status=status,
                operation_id=f"00000000-0000-4000-8000-{sequence:012d}",
            )
        )


class DurableEnsureListTests(unittest.TestCase):
    @staticmethod
    def facade(
        bridge_call: Any,
        support: Path,
    ) -> V2CoreFacade:
        return V2CoreFacade(
            make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
        )

    def call_concurrently(
        self,
        facades: list[V2CoreFacade],
        arguments: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], str | None]]:
        ready = threading.Barrier(len(facades))

        def invoke(index: int) -> tuple[dict[str, Any], str | None]:
            ready.wait(timeout=2)
            return facades[index].call_with_state(
                "ensure_reminder_list",
                arguments[index],
            )

        with ThreadPoolExecutor(max_workers=len(facades)) as pool:
            return list(pool.map(invoke, range(len(facades))))

    def test_same_key_across_facade_graphs_creates_once_and_replays_identity(
        self,
    ) -> None:
        bridge = RacingEnsureListBridge()
        arguments = {
            "source_id": "SOURCE-1",
            "name": "  Work  ",
            "idempotency_key": "ensure:work:shared",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            results = self.call_concurrently(
                [self.facade(bridge, support), self.facade(bridge, support)],
                [copy.deepcopy(arguments), copy.deepcopy(arguments)],
            )

        payloads = [payload for payload, _ in results]
        states = [state for _, state in results]
        self.assertEqual(len(bridge.created_lists), 1)
        self.assertEqual(len(bridge.calls), 1)
        self.assertCountEqual(
            [payload.get("replayed", False) for payload in payloads],
            [False, True],
        )
        self.assertEqual(
            {payload["target"]["list_id"] for payload in payloads},
            {"LIST-1"},
        )
        self.assertEqual(
            {payload["operation_id"] for payload in payloads},
            {"00000000-0000-4000-8000-000000000001"},
        )
        self.assertEqual(states, ["committed", "committed"])
        for payload in payloads:
            self.assertEqual(payload["after"]["title"], "Work")
            self.assertEqual(payload["after"]["source"]["id"], "SOURCE-1")
            validate_public_result("ensure_reminder_list", payload, "committed")

    def test_same_name_with_different_keys_serializes_and_rechecks_current_state(
        self,
    ) -> None:
        bridge = RacingEnsureListBridge()
        common = {"source_id": "SOURCE-1", "name": "Work"}

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            results = self.call_concurrently(
                [self.facade(bridge, support), self.facade(bridge, support)],
                [
                    {**common, "idempotency_key": "ensure:work:first"},
                    {**common, "idempotency_key": "ensure:work:second"},
                ],
            )
            conflict = self.facade(bridge, support).ensure_reminder_list(
                {
                    "source_id": "SOURCE-1",
                    "name": "Home",
                    "idempotency_key": "ensure:work:first",
                }
            )

        payloads = [payload for payload, _ in results]
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(
            bridge.created_lists,
            [{"id": "LIST-1", "source_id": "SOURCE-1", "title": "Work"}],
        )
        self.assertCountEqual(
            [payload["status"] for payload in payloads], ["verified", "unchanged"]
        )
        self.assertEqual([payload["replayed"] for payload in payloads], [False, False])
        self.assertEqual(
            {payload["target"]["list_id"] for payload in payloads},
            {"LIST-1"},
        )
        self.assertEqual(
            conflict["error"]["reason_code"],
            "idempotency_key_conflict",
        )
        self.assertEqual(
            conflict["idempotency_key_hash"],
            payloads[0]["idempotency_key_hash"],
        )
        self.assertCountEqual(
            [state for _payload, state in results], ["committed", "not_mutated"]
        )
        for payload, state in results:
            validate_public_result("ensure_reminder_list", payload, state)

    def test_fresh_key_rechecks_after_the_previously_verified_list_disappears(
        self,
    ) -> None:
        bridge_call = mock.Mock(
            side_effect=[
                transport(
                    ensure_list_receipt(
                        list_id="LIST-1",
                        source_id="SOURCE-1",
                        name="Work",
                        status="verified",
                        operation_id="00000000-0000-4000-8000-000000000001",
                    )
                ),
                transport(
                    ensure_list_receipt(
                        list_id="LIST-2",
                        source_id="SOURCE-1",
                        name="Work",
                        status="verified",
                        operation_id="00000000-0000-4000-8000-000000000002",
                    )
                ),
            ]
        )
        common = {"source_id": "SOURCE-1", "name": "Work"}

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first = self.facade(bridge_call, support).ensure_reminder_list(
                {**common, "idempotency_key": "ensure:work:first"}
            )
            current = self.facade(bridge_call, support).ensure_reminder_list(
                {**common, "idempotency_key": "ensure:work:after-delete"}
            )

        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(first["target"]["list_id"], "LIST-1")
        self.assertEqual(current["target"]["list_id"], "LIST-2")
        self.assertFalse(current["replayed"])

    def test_same_key_with_different_normalized_input_conflicts_without_dispatch(
        self,
    ) -> None:
        bridge = RacingEnsureListBridge()
        key = "ensure:normalized-input:shared"

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first_facade = self.facade(bridge, support)
            second_facade = self.facade(bridge, support)
            first, first_state = first_facade.call_with_state(
                "ensure_reminder_list",
                {
                    "source_id": " SOURCE-1 ",
                    "name": "  Work  ",
                    "idempotency_key": key,
                },
            )
            conflict, conflict_state = second_facade.call_with_state(
                "ensure_reminder_list",
                {
                    "source_id": "SOURCE-1",
                    "name": " Home ",
                    "idempotency_key": key,
                },
            )

        self.assertEqual(first["status"], "verified")
        self.assertEqual(first_state, "committed")
        self.assertEqual(conflict["status"], "failed_no_mutation")
        self.assertEqual(conflict_state, "not_mutated")
        self.assertEqual(
            conflict["error"]["reason_code"],
            "idempotency_key_conflict",
        )
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(len(bridge.created_lists), 1)
        validate_public_result(
            "ensure_reminder_list",
            conflict,
            "not_mutated",
        )

    def test_durable_replay_keeps_name_private_and_reconstructs_public_list(
        self,
    ) -> None:
        bridge = RacingEnsureListBridge()
        name = "Private Roadmap"
        key = "ensure:private-roadmap:shared"
        arguments = {
            "source_id": "SOURCE-PRIVATE",
            "name": name,
            "idempotency_key": key,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first, first_state = self.facade(bridge, support).call_with_state(
                "ensure_reminder_list",
                arguments,
            )
            replay, replay_state = self.facade(bridge, support).call_with_state(
                "ensure_reminder_list",
                arguments,
            )
            store_path = support / "idempotency.json"
            self.assertTrue(
                store_path.exists(),
                "ensure_reminder_list did not enter the durable idempotency Module",
            )
            store_text = store_path.read_text(encoding="utf-8")

        self.assertNotIn(name, store_text)
        self.assertNotIn(key, store_text)
        self.assertFalse(first.get("replayed", False))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["operation_id"], first["operation_id"])
        self.assertEqual(replay["target"], first["target"])
        self.assertEqual(replay["after"]["title"], name)
        self.assertEqual(replay["after"]["source"]["id"], "SOURCE-PRIVATE")
        self.assertEqual(first["after"]["source"]["title"], "Test Account")
        self.assertNotIn("title", replay["after"]["source"])
        self.assertEqual(first_state, "committed")
        self.assertEqual(replay_state, "committed")
        self.assertEqual(len(bridge.calls), 1)
        validate_public_result("ensure_reminder_list", replay, "committed")

    def test_parent_proven_not_started_clears_ensure_list_fence(self) -> None:
        failure = {
            "schema_version": 1,
            "ok": False,
            "status": "failed_no_mutation",
            "operation": "ensure_reminder_list",
            "target": {"source_id": "SOURCE-1", "list_id": None},
            "before": {},
            "after": {},
            "verification": {
                "state": "not_needed",
                "write_performed": False,
                "final_read": False,
            },
            "recovery": {
                "semantics": "retry_after_environment_repair",
                "automatic_retry_safe": True,
            },
            "error": {
                "code": "eventkit_bridge_unavailable",
                "reason_code": "eventkit_helper_build_not_started",
                "message": "The EventKit helper did not start.",
                "retryable": True,
            },
        }
        bridge_call = mock.Mock(
            return_value=transport(
                copy.deepcopy(failure),
                proves_not_started=True,
            )
        )
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:helper-build:retry",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first = self.facade(bridge_call, support).ensure_reminder_list(arguments)
            second = self.facade(bridge_call, support).ensure_reminder_list(arguments)
            store_path = support / "idempotency.json"
            self.assertTrue(
                store_path.exists(),
                "a proven-not-started ensure never created its durable fence",
            )
            stored = json.loads(store_path.read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "failed_no_mutation")
        self.assertEqual(second["status"], "failed_no_mutation")
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored, {"version": 1, "entries": {}})

    def test_uncertain_ensure_list_failures_remain_fenced_without_redispatch(
        self,
    ) -> None:
        cases = {
            "timeout": transport(
                {
                    "ok": False,
                    "error": {
                        "code": "eventkit_timeout",
                        "message": "The EventKit helper timed out.",
                    },
                }
            ),
            "malformed": transport(
                {"ok": True, "unexpected": "shape"},
                is_error=False,
            ),
            "identity_mismatch": transport(
                ensure_list_receipt(
                    list_id="LIST-WRONG",
                    source_id="SOURCE-1",
                    name="Wrong Name",
                    status="verified",
                    operation_id="00000000-0000-4000-8000-999999999999",
                )
            ),
        }
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:uncertain:shared",
        }

        for label, transport_result in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temp_dir:
                support = Path(temp_dir) / "support"
                bridge_call = mock.Mock(return_value=transport_result)
                first, first_state = self.facade(
                    bridge_call,
                    support,
                ).call_with_state("ensure_reminder_list", arguments)
                replay, replay_state = self.facade(
                    bridge_call,
                    support,
                ).call_with_state("ensure_reminder_list", arguments)
                store_path = support / "idempotency.json"
                self.assertTrue(
                    store_path.exists(),
                    "an uncertain ensure outcome did not retain a durable fence",
                )
                stored = json.loads(store_path.read_text(encoding="utf-8"))

                self.assertEqual(first["status"], "committed_verification_pending")
                self.assertEqual(first_state, "unknown")
                self.assertEqual(replay["status"], "committed_verification_pending")
                self.assertEqual(replay_state, "unknown")
                self.assertTrue(replay["replayed"])
                self.assertEqual(bridge_call.call_count, 1)
                self.assertEqual(
                    next(iter(stored["entries"].values()))["state"],
                    "in_progress",
                )
                validate_public_result("ensure_reminder_list", replay, "unknown")

    def test_pending_ensure_receipt_replays_with_a_valid_warning(self) -> None:
        pending = {
            "schema_version": 1,
            "ok": True,
            "status": "committed_verification_pending",
            "operation": "ensure_reminder_list",
            "operation_id": "00000000-0000-4000-8000-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"source_id": "SOURCE-1", "list_id": None},
            "before": {},
            "after": {},
            "verification": {"state": "pending", "write_performed": None},
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                {"code": "eventkit_list_read_back_pending", "message": "Native wording"}
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": "eventkit_final_read_pending",
                "message": "Native wording",
                "retryable": False,
            },
        }
        bridge_call = mock.Mock(return_value=transport(pending, is_error=False))
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:pending-replay",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first, first_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list", arguments
            )
            replay, replay_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list", arguments
            )

        self.assertEqual(bridge_call.call_count, 1)
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first_state, "unknown")
        self.assertEqual(replay_state, "unknown")
        self.assertEqual(
            replay["warnings"][0]["code"],
            "eventkit_list_read_back_pending",
        )
        validate_public_result("ensure_reminder_list", replay, "unknown")

    def test_uncertain_identity_blocks_a_different_key_without_redispatch(self) -> None:
        bridge_call = mock.Mock(
            return_value=transport(
                {
                    "ok": False,
                    "error": {
                        "code": "eventkit_timeout",
                        "message": "The EventKit helper timed out.",
                    },
                }
            )
        )
        common = {"source_id": "SOURCE-1", "name": "Work"}

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first, first_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {**common, "idempotency_key": "ensure:work:first"},
            )
            blocked, blocked_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {**common, "idempotency_key": "ensure:work:second"},
            )
            conflict = self.facade(bridge_call, support).ensure_reminder_list(
                {
                    "source_id": "SOURCE-1",
                    "name": "Home",
                    "idempotency_key": "ensure:work:second",
                }
            )

        self.assertEqual(bridge_call.call_count, 1)
        self.assertEqual(first["status"], "committed_verification_pending")
        self.assertEqual(blocked["status"], "committed_verification_pending")
        self.assertIsNone(blocked["before"])
        self.assertIsNone(blocked["after"])
        self.assertTrue(blocked["replayed"])
        self.assertEqual(conflict["error"]["reason_code"], "idempotency_key_conflict")
        self.assertEqual(first_state, "unknown")
        self.assertEqual(blocked_state, "unknown")
        validate_public_result("ensure_reminder_list", blocked, "unknown")

    def test_corrupt_unresolved_ensure_identity_fails_closed(self) -> None:
        bridge_call = mock.Mock(
            return_value=transport(
                ensure_list_receipt(
                    list_id="LIST-1",
                    source_id="SOURCE-1",
                    name="Work",
                    status="verified",
                    operation_id="00000000-0000-4000-8000-000000000001",
                )
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            support.mkdir()
            (support / "idempotency.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "a" * 64: {
                                "operation": "eventkit_ensure_reminder_list",
                                "created_at_epoch": 1_000.0,
                                "state": "in_progress",
                                "operation_id": (
                                    "00000000-0000-4000-8000-999999999999"
                                ),
                            }
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            payload, state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {
                    "source_id": "SOURCE-1",
                    "name": "Work",
                    "idempotency_key": "ensure:corrupt-identity",
                },
            )

        bridge_call.assert_not_called()
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")

    def test_live_ensure_rejects_a_mismatched_target_source(self) -> None:
        receipt = ensure_list_receipt(
            list_id="LIST-1",
            source_id="SOURCE-1",
            name="Work",
            status="verified",
            operation_id="00000000-0000-4000-8000-000000000001",
        )
        receipt["target"]["source_id"] = "SOURCE-WRONG"
        bridge_call = mock.Mock(return_value=transport(receipt))

        with tempfile.TemporaryDirectory() as temp_dir:
            payload, state = self.facade(
                bridge_call,
                Path(temp_dir) / "support",
            ).call_with_state(
                "ensure_reminder_list",
                {
                    "source_id": "SOURCE-1",
                    "name": "Work",
                    "idempotency_key": "ensure:wrong-target-source",
                },
            )

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")
        self.assertEqual(bridge_call.call_count, 1)

    def test_unchanged_ensure_rejects_changed_safe_metadata(self) -> None:
        receipt = ensure_list_receipt(
            list_id="LIST-1",
            source_id="SOURCE-1",
            name="Work",
            status="unchanged",
            operation_id="00000000-0000-4000-8000-000000000001",
        )
        receipt["before"]["allows_content_modifications"] = False
        bridge_call = mock.Mock(return_value=transport(receipt))

        with tempfile.TemporaryDirectory() as temp_dir:
            payload, state = self.facade(
                bridge_call, Path(temp_dir) / "support"
            ).call_with_state(
                "ensure_reminder_list",
                {
                    "source_id": "SOURCE-1",
                    "name": "Work",
                    "idempotency_key": "ensure:contradictory-unchanged",
                },
            )

        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")

    def test_replay_validates_stored_source_before_rehydrating_title(self) -> None:
        bridge_call = mock.Mock(
            return_value=transport(
                ensure_list_receipt(
                    list_id="LIST-1",
                    source_id="SOURCE-1",
                    name="Work",
                    status="verified",
                    operation_id="00000000-0000-4000-8000-000000000001",
                )
            )
        )
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:tampered-source",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first = self.facade(bridge_call, support).ensure_reminder_list(arguments)
            store_path = support / "idempotency.json"
            stored = json.loads(store_path.read_text(encoding="utf-8"))
            record = next(iter(stored["entries"].values()))
            future = record["created_at_epoch"] + 32 * 86400
            record["result"]["target"]["source_id"] = "SOURCE-WRONG"
            record["result"]["after"]["source"]["id"] = "SOURCE-WRONG"
            store_path.write_text(
                json.dumps(stored, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            replay, replay_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                arguments,
            )
            other_key, other_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {**arguments, "idempotency_key": "ensure:tampered-source:other"},
            )
            with mock.patch.object(
                durable_idempotency._time,
                "time",
                return_value=future,
            ):
                retained, retained_state = self.facade(
                    bridge_call,
                    support,
                ).call_with_state(
                    "ensure_reminder_list",
                    {**arguments, "idempotency_key": "ensure:tampered-source:future"},
                )

        self.assertEqual(first["status"], "verified")
        self.assertEqual(replay["status"], "committed_verification_pending")
        self.assertEqual(replay_state, "unknown")
        self.assertEqual(other_key["status"], "committed_verification_pending")
        self.assertEqual(other_state, "unknown")
        self.assertEqual(retained["status"], "committed_verification_pending")
        self.assertEqual(retained_state, "unknown")
        self.assertEqual(bridge_call.call_count, 1)

    def test_same_key_with_a_corrupt_input_hash_stays_outcome_unknown(self) -> None:
        key = "ensure:corrupt-same-key"
        bridge_call = mock.Mock()
        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            support.mkdir()
            (support / "idempotency.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            durable_idempotency.idempotency_key_hash(
                                "eventkit_ensure_reminder_list", key
                            ): {
                                "operation": "eventkit_ensure_reminder_list",
                                "created_at_epoch": 1_000.0,
                                "state": "in_progress",
                                "operation_id": "00000000-0000-4000-8000-999999999999",
                            }
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            payload, state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": key},
            )

        bridge_call.assert_not_called()
        self.assertEqual(payload["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")

    def test_complete_ensure_with_a_corrupt_input_hash_blocks_a_fresh_key(
        self,
    ) -> None:
        bridge_call = mock.Mock(
            return_value=transport(
                ensure_list_receipt(
                    list_id="LIST-1",
                    source_id="SOURCE-1",
                    name="Work",
                    status="verified",
                    operation_id="00000000-0000-4000-8000-000000000001",
                )
            )
        )
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:corrupt-complete:first",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first = self.facade(bridge_call, support).ensure_reminder_list(arguments)
            store_path = support / "idempotency.json"
            stored = json.loads(store_path.read_text(encoding="utf-8"))
            next(iter(stored["entries"].values()))["input_hash"] = "corrupt"
            store_path.write_text(
                json.dumps(stored, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            blocked, state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list",
                {
                    **arguments,
                    "idempotency_key": "ensure:corrupt-complete:fresh",
                },
            )

        self.assertEqual(first["status"], "verified")
        self.assertEqual(blocked["status"], "committed_verification_pending")
        self.assertEqual(state, "unknown")
        self.assertEqual(bridge_call.call_count, 1)

    def test_verified_and_unchanged_replays_preserve_safe_list_metadata(self) -> None:
        for status in ("verified", "unchanged"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                receipt = ensure_list_receipt(
                    list_id="LIST-1",
                    source_id="SOURCE-1",
                    name="Work",
                    status=status,
                    operation_id="00000000-0000-4000-8000-000000000001",
                )
                if status == "unchanged":
                    receipt["warnings"] = [
                        {
                            "code": "duplicate_list_name_in_source",
                            "message": "untrusted native wording",
                        }
                    ]
                bridge_call = mock.Mock(return_value=transport(receipt))
                support = Path(temp_dir) / "support"
                arguments = {
                    "source_id": "SOURCE-1",
                    "name": "Work",
                    "idempotency_key": f"ensure:metadata:{status}",
                }
                first = self.facade(bridge_call, support).ensure_reminder_list(arguments)
                replay = self.facade(bridge_call, support).ensure_reminder_list(arguments)

                for field in (
                    "id",
                    "title",
                    "type",
                    "allows_content_modifications",
                    "subscribed",
                    "immutable",
                ):
                    self.assertEqual(replay["after"][field], first["after"][field])
                for field in ("id", "type", "is_delegate", "reminder_list_count"):
                    self.assertEqual(
                        replay["after"]["source"][field],
                        first["after"]["source"][field],
                    )
                if status == "unchanged":
                    self.assertEqual(replay["before"]["title"], "Work")
                    self.assertEqual(
                        replay["warnings"],
                        [
                            {
                                "code": "duplicate_list_name_in_source",
                                "message": (
                                    "More than one reminder list in this source has the exact "
                                    "name; the first stable identifier was returned."
                                ),
                            }
                        ],
                    )
                self.assertEqual(bridge_call.call_count, 1)

    def test_fresh_bridge_cannot_spoof_replayed_provenance(self) -> None:
        receipt = ensure_list_receipt(
            list_id="LIST-1",
            source_id="SOURCE-1",
            name="Work",
            status="verified",
            operation_id="00000000-0000-4000-8000-000000000001",
        )
        receipt["replayed"] = True
        bridge_call = mock.Mock(return_value=transport(receipt))

        with tempfile.TemporaryDirectory() as temp_dir:
            payload = self.facade(
                bridge_call,
                Path(temp_dir) / "support",
            ).ensure_reminder_list(
                {
                    "source_id": "SOURCE-1",
                    "name": "Work",
                    "idempotency_key": "ensure:spoofed-replay",
                }
            )

        self.assertFalse(payload["replayed"])

    def test_same_key_uses_one_hash_on_success_conflict_unknown_and_replay(self) -> None:
        verified_bridge = mock.Mock(
            return_value=transport(
                ensure_list_receipt(
                    list_id="LIST-1",
                    source_id="SOURCE-1",
                    name="Work",
                    status="verified",
                    operation_id="00000000-0000-4000-8000-000000000001",
                )
            )
        )
        uncertain_bridge = mock.Mock(
            return_value=transport(
                {
                    "ok": False,
                    "error": {"code": "eventkit_timeout", "message": "timed out"},
                }
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            verified_support = Path(temp_dir) / "verified"
            key = "ensure:stable-public-hash"
            first = self.facade(verified_bridge, verified_support).ensure_reminder_list(
                {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": key}
            )
            conflict = self.facade(verified_bridge, verified_support).ensure_reminder_list(
                {"source_id": "SOURCE-1", "name": "Home", "idempotency_key": key}
            )
            uncertain_support = Path(temp_dir) / "uncertain"
            unknown = self.facade(uncertain_bridge, uncertain_support).ensure_reminder_list(
                {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": key}
            )
            replay = self.facade(uncertain_bridge, uncertain_support).ensure_reminder_list(
                {"source_id": "SOURCE-1", "name": "Work", "idempotency_key": key}
            )

        self.assertEqual(first["idempotency_key_hash"], conflict["idempotency_key_hash"])
        self.assertEqual(unknown["idempotency_key_hash"], replay["idempotency_key_hash"])

    def test_malformed_parent_no_dispatch_proof_clears_the_fence(self) -> None:
        bridge_call = mock.Mock(
            return_value=transport(
                {"ok": False, "error": {}},
                proves_not_started=True,
            )
        )
        arguments = {
            "source_id": "SOURCE-1",
            "name": "Work",
            "idempotency_key": "ensure:malformed-parent-proof",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            first, first_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list", arguments
            )
            second, second_state = self.facade(bridge_call, support).call_with_state(
                "ensure_reminder_list", arguments
            )
            stored = json.loads((support / "idempotency.json").read_text(encoding="utf-8"))

        for payload in (first, second):
            self.assertEqual(payload["status"], "failed_no_mutation")
            self.assertEqual(
                payload["error"]["reason_code"],
                "invalid_prelaunch_failure_payload",
            )
        self.assertEqual(first_state, "not_mutated")
        self.assertEqual(second_state, "not_mutated")
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored["entries"], {})


class CoreBackendInterfaceTests(unittest.TestCase):
    def test_server_composes_core_without_in_process_backend_loaders(self) -> None:
        self.assertFalse(hasattr(mcp_server, "_ADAPTER_MODULE"))
        self.assertFalse(hasattr(mcp_server, "bundled_adapter_module"))
        self.assertFalse(hasattr(mcp_server, "_EVENTKIT_BRIDGE_MODULE"))
        self.assertFalse(hasattr(mcp_server, "bundled_eventkit_bridge_module"))
        self.assertFalse(hasattr(mcp_server, "_load_local_module"))
        dispatch = mcp_server._LocalToolDispatch(mcp_server.DEFAULT_BACKEND_PATHS)

        with (
            mock.patch("mcp.v2_core_backend.CoreBackend") as backend_type,
            mock.patch("mcp.v2_core.V2CoreFacade") as facade_type,
        ):
            facade = dispatch.core_facade()

        self.assertIs(facade, facade_type.return_value)
        kwargs = backend_type.call_args.kwargs
        self.assertIs(kwargs["idempotency_call"], mcp_server.execute_idempotent)
        self.assertNotIn("adapter_module", kwargs)
        self.assertNotIn("bridge_module", kwargs)

    def test_create_uses_narrow_idempotency_without_importing_adapter(self) -> None:
        def loaded_adapter_modules() -> set[str]:
            return {
                name
                for name, module in sys.modules.items()
                if Path(str(getattr(module, "__file__", ""))).name
                == "reminders_adapter.py"
            }

        payload = valid_eventkit_receipt("create_reminder")
        idempotency_call = mock.Mock(side_effect=idempotency_passthrough)
        backend = make_backend(
            bridge_call=mock.Mock(return_value=transport(payload)),
            idempotency_call=idempotency_call,
        )
        before = loaded_adapter_modules()

        reply = backend.invoke(
            "create_reminder",
            {
                "calendar_id": "LIST-1",
                "title": "Narrow dependency",
                "idempotency_key": "narrow-idempotency",
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(loaded_adapter_modules(), before)
        idempotency_call.assert_called_once()

    def test_update_mutation_does_not_call_idempotency(self) -> None:
        payload = valid_eventkit_receipt("update_reminder")
        idempotency_call = mock.Mock(
            side_effect=RuntimeError("idempotency unavailable")
        )
        backend = CoreBackend(
            bridge_call=mock.Mock(return_value=transport(payload)),
            adapter_call=mock.Mock(),
            build_adapter_argv=mock.Mock(),
            idempotency_call=idempotency_call,
            receipt_validator=mock.Mock(return_value=None),
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T00:00:00Z",
                "patch": {"title": "Changed"},
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(reply.payload, payload)
        idempotency_call.assert_not_called()

    def test_create_proven_no_write_bridge_failure_clears_fence_for_retry(
        self,
    ) -> None:
        failed = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Full Reminders access is required",
                "category": "permission_denied",
                "retryable": False,
                "details": {},
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Safe retry",
            "idempotency_key": "create-no-write-retry",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            store = support / "idempotency.json"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            second = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            stored = json.loads(store.read_text(encoding="utf-8"))

        self.assertTrue(first.is_error)
        self.assertTrue(second.is_error)
        self.assertEqual(without_durable_metadata(first.payload), failed)
        self.assertEqual(without_durable_metadata(second.payload), failed)
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored["entries"], {})

    def test_create_child_spoofed_no_write_flags_remain_fenced(self) -> None:
        failed = {
            "ok": False,
            "__dispatch_phase": "not_started",
            "mutation_not_started": True,
            "error": {
                "code": "eventkit_bridge_unavailable",
                "message": "The bundled EventKit bridge is unavailable.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Unclassified failure",
            "idempotency_key": "create-unknown-failure",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )
            replay = backend.invoke(
                "create_reminder",
                arguments,
                mutation=True,
            )

        self.assertTrue(first.is_error)
        self.assertEqual(without_durable_metadata(first.payload), failed)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertEqual(replay.mutation_state, "unknown")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_parent_proven_prelaunch_failure_clears_fence(self) -> None:
        not_started = {
            "ok": False,
            "error": {
                "code": "eventkit_bridge_unavailable",
                "message": "The bundled EventKit bridge is unavailable.",
            },
        }
        bridge_call = mock.Mock(
            return_value=transport(
                copy.deepcopy(not_started),
                proves_not_started=True,
            )
        )
        arguments = {
            "list_id": "LIST-1",
            "title": "Retry prelaunch",
            "idempotency_key": "create-parent-prelaunch",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            store = support / "idempotency.json"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            facade = V2CoreFacade(backend)
            first, first_state = facade.call_with_state(
                "create_reminder", arguments
            )
            second, second_state = facade.call_with_state(
                "create_reminder", arguments
            )
            stored = json.loads(store.read_text(encoding="utf-8"))

        for receipt in (first, second):
            self.assertFalse(receipt["ok"])
            self.assertEqual(receipt["status"], "failed_no_mutation")
            self.assertEqual(receipt["operation"], "create_reminder")
            self.assertFalse(receipt["verification"]["write_performed"])
            self.assertNotIn("__dispatch_phase", repr(receipt))
        self.assertEqual(bridge_call.call_count, 2)
        self.assertEqual(stored["entries"], {})
        self.assertEqual(first_state, "not_mutated")
        self.assertEqual(second_state, "not_mutated")

    def test_create_pending_bridge_receipt_replays_without_redispatch(self) -> None:
        pending = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "committed_verification_pending",
            "ok": True,
            "operation_id": "11111111-1111-4111-8111-111111111111",
            "backend": "eventkit_public_sdk",
            "target": {},
            "after": {},
            "verification": {"state": "pending", "write_performed": None},
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                {
                    "code": "verification_pending",
                    "message": "Read before retrying.",
                }
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": "native_timeout",
                "message": "The EventKit outcome is unknown.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(pending)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Pending create",
            "idempotency_key": "create-pending",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)
            public, public_state = V2CoreFacade(backend).call_with_state(
                "create_reminder",
                {
                    "list_id": "LIST-1",
                    "title": "Pending create",
                    "idempotency_key": "create-pending",
                },
            )

        self.assertFalse(first.is_error)
        self.assertEqual(first.payload["status"], "committed_verification_pending")
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(first.payload.get("replayed", False))
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertEqual(replay.mutation_state, "unknown")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)
        self.assertEqual(public_state, "unknown")
        self.assertEqual(public["warnings"][0]["code"], "verification_pending")
        validate_public_result("create_reminder", public, "unknown")

    def test_create_pending_replay_preserves_durable_privacy_warning(self) -> None:
        pending = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "committed_verification_pending",
            "ok": True,
            "operation_id": "11111111-1111-4111-8111-111111111111",
            "backend": "idempotency_fence",
            "target": {},
            "before": {},
            "after": {},
            "verification": {
                "state": "pending",
                "write_performed": None,
                "final_read": False,
            },
            "recovery": {
                "semantics": "read_before_retry",
                "automatic_retry_safe": False,
            },
            "warnings": [
                *[
                    {"code": f"existing_warning_{index}", "message": "Existing."}
                    for index in range(20)
                ],
                {
                    "code": "idempotency_privacy_scrub_failed",
                    "message": "Older retry content could not be scrubbed.",
                }
            ],
            "error": {
                "code": "sync_pending",
                "reason_code": "idempotency_outcome_unknown",
                "message": "Read before retrying.",
                "retryable": False,
            },
            "replayed": True,
        }

        def replay(**_arguments: Any) -> dict[str, Any]:
            return copy.deepcopy(pending)

        backend = make_backend(
            bridge_call=mock.Mock(),
            idempotency_call=replay,
        )
        reply = backend.invoke(
            "create_reminder",
            {
                "calendar_id": "LIST-1",
                "title": "Pending create",
                "idempotency_key": "create-pending-warning",
            },
            mutation=True,
        )

        warning_codes = [warning["code"] for warning in reply.payload["warnings"]]
        self.assertIn("idempotency_privacy_scrub_failed", warning_codes)
        self.assertIn("verification_pending", warning_codes)
        self.assertEqual(len(warning_codes), 20)

    def test_create_committed_projection_replay_preserves_state(self) -> None:
        verified = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "create_reminder",
            "operation_id": "12345678-1234-4234-9234-1234567890ab",
            "backend": "eventkit_public_sdk",
            "target": {"calendar_id": "LIST-1"},
            "after": {"calendar_id": "LIST-1"},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": False,
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(verified)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Committed replay",
            "url": "https://example.com/item",
            "idempotency_key": "create-committed-replay",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertEqual(first.payload["status"], "partial_success")
        self.assertEqual(first.mutation_state, "committed")
        self.assertEqual(replay.payload["status"], "partial_success")
        self.assertEqual(replay.mutation_state, "committed")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_replay_stays_committed_after_private_recurrence_redaction(
        self,
    ) -> None:
        verified = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "create_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"reminder_id": "R-RECURRENCE"},
            "after": {
                "reminder_id": "R-RECURRENCE",
                "recurrence_rules": [
                    {
                        "frequency": "monthly",
                        "days_of_month": [5, 20],
                        "end": {"count": 12},
                    }
                ],
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
                "target_fields": ["title", "recurrence_rules"],
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": False,
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(verified)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Private recurring schedule",
            "idempotency_key": "create-private-recurrence",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(Path(temp_dir) / "support"),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertEqual(first.mutation_state, "committed")
        self.assertIn("recurrence_rules", first.payload["after"])
        self.assertEqual(replay.mutation_state, "committed")
        self.assertEqual(replay.payload["after"], {"reminder_id": "R-RECURRENCE"})
        self.assertNotIn("target_fields", replay.payload["verification"])
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_invalid_success_receipt_remains_fenced(self) -> None:
        invalid = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "verified",
            "ok": True,
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(invalid)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Invalid receipt",
            "idempotency_key": "create-invalid-success",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload["error"]["code"], "invalid_eventkit_receipt")
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_validator_rejected_no_write_label_remains_fenced(self) -> None:
        rejected = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "untrusted_error_code",
                "message": "This label did not cross the bridge contract.",
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(rejected)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Rejected label",
            "idempotency_key": "create-rejected-label",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(without_durable_metadata(first.payload), rejected)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_contradictory_no_write_evidence_remains_fenced(self) -> None:
        contradictory = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "after": {"id": REMINDER_ID},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
            },
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Contradictory write evidence",
                "retryable": False,
            },
        }
        bridge_call = mock.Mock(
            return_value=transport(copy.deepcopy(contradictory))
        )
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Contradictory no-write",
            "idempotency_key": "create-contradictory-no-write",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            first = backend.invoke("create_reminder", arguments, mutation=True)
            replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(without_durable_metadata(first.payload), contradictory)
        self.assertEqual(first.mutation_state, "unknown")
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_create_no_write_cleanup_failure_keeps_fence_fail_closed(self) -> None:
        failed = {
            "schema_version": 1,
            "operation": "create_reminder",
            "status": "failed_no_mutation",
            "ok": False,
            "error": {
                "code": "permission_denied",
                "reason_code": "reminders_access_denied",
                "message": "Full Reminders access is required",
                "category": "permission_denied",
                "retryable": False,
                "details": {},
            },
        }
        bridge_call = mock.Mock(return_value=transport(copy.deepcopy(failed)))
        arguments = {
            "calendar_id": "LIST-1",
            "title": "Cleanup failure",
            "idempotency_key": "create-cleanup-failure",
        }
        writes = 0
        real_write = durable_idempotency._write_store

        def fail_cleanup(
            payload: dict[str, Any],
            *,
            storage_dir: Path,
            store_path: Path,
        ) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("disk full")
            real_write(
                payload,
                storage_dir=storage_dir,
                store_path=store_path,
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            support = Path(temp_dir) / "support"
            backend = make_backend(
                bridge_call=bridge_call,
                idempotency_call=bound_idempotency(support),
            )
            with mock.patch.object(
                durable_idempotency,
                "_write_store",
                side_effect=fail_cleanup,
            ):
                first = backend.invoke("create_reminder", arguments, mutation=True)
                replay = backend.invoke("create_reminder", arguments, mutation=True)

        self.assertTrue(first.is_error)
        self.assertEqual(first.payload["status"], "failed_no_mutation")
        self.assertEqual(
            first.payload["error"]["details"]["reason_code"],
            "idempotency_fence_cleanup_failed",
        )
        self.assertFalse(replay.is_error)
        self.assertEqual(replay.payload["status"], "committed_verification_pending")
        self.assertTrue(replay.payload["replayed"])
        self.assertEqual(bridge_call.call_count, 1)

    def test_maps_reads_and_mutations_without_rewriting_arguments(self) -> None:
        read_payload = {
            "schema_version": 1,
            "operation": "fetch_reminders",
            "status": "verified",
            "ok": True,
            "data": {"items": []},
        }
        mutation_payload = valid_eventkit_receipt("update_reminder")
        bridge_call = mock.Mock(
            side_effect=[transport(read_payload), transport(mutation_payload)]
        )
        backend = make_backend(bridge_call=bridge_call)
        read_arguments = {
            "calendar_ids": ["LIST-1"],
            "limit": 10,
            "offset": 37,
        }
        mutation_arguments = {
            "reminder_id": REMINDER_ID,
            "expected_last_modified": "2026-08-25T00:00:00Z",
            "patch": {"title": "Changed"},
        }

        read_reply = backend.invoke(
            "fetch_reminders",
            read_arguments,
            mutation=False,
        )
        mutation_reply = backend.invoke(
            "update_reminder",
            mutation_arguments,
            mutation=True,
        )

        self.assertEqual(read_reply.payload, read_payload)
        self.assertFalse(read_reply.is_error)
        self.assertIsNone(read_reply.mutation_state)
        self.assertEqual(mutation_reply.payload, mutation_payload)
        self.assertFalse(mutation_reply.is_error)
        self.assertEqual(mutation_reply.mutation_state, "committed")
        self.assertEqual(
            bridge_call.call_args_list,
            [
                mock.call("fetch_reminders", read_arguments),
                mock.call("update_reminder", mutation_arguments),
            ],
        )

    def test_hybrid_url_failure_preserves_partial_receipt_and_final_read_order(
        self,
    ) -> None:
        url = "https://example.com/spec"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {},
            "after": {
                "id": REMINDER_ID,
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": "2026-08-25T01:00:00.000Z",
            },
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[
                transport(copy.deepcopy(eventkit_receipt)),
                transport(final_read),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "error": {
                            "code": "native_url_attachment_failed",
                            "message": "The native URL attachment was not saved.",
                        },
                    },
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertFalse(reply.is_error)
        self.assertEqual(reply.payload["status"], "partial_success")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "native_url_attachment_failed",
        )
        self.assertFalse(reply.payload["recovery"]["automatic_retry_safe"])
        self.assertEqual(reply.payload["verification"]["state"], "partial")
        self.assertTrue(reply.payload["verification"]["final_read"])
        self.assertEqual(
            [call.args[0] for call in bridge_call.call_args_list],
            ["update_reminder", "read_reminder"],
        )
        self.assertEqual(
            argv_calls,
            [
                (
                    "list_reminder_attachments",
                    {
                        "reminder_id": REMINDER_ID,
                        "attachment_type": "url",
                        "limit": 200,
                    },
                ),
                (
                    "attach_url_to_reminder",
                    {
                        "reminder_id": REMINDER_ID,
                        "url": url,
                        "if_version": 7,
                    },
                ),
            ],
        )

    def test_url_patch_replaces_single_attachment_matching_previous_metadata(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "33333333-3333-4333-8333-333333333333",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [
                            {"id": "ATTACHMENT-A", "type": "url", "url": old_url},
                            {
                                "id": "UNRELATED",
                                "type": "url",
                                "url": "https://example.com/unrelated",
                            },
                        ],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "replace_attachment",
                        "operation_id": "44444444-4444-4444-8444-444444444444",
                        "backend": "sqlite_private",
                        "target": {
                            "reminder_id": REMINDER_ID,
                            "attachment_id": "ATTACHMENT-B",
                        },
                        "before": {},
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "replace_previous_attachment"},
                    }
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": new_url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "verified")
        self.assertEqual(reply.payload["after"]["url_attachment"]["url"], new_url)
        replace_tool, replace_arguments = argv_calls[1]
        self.assertEqual(replace_tool, "replace_reminder_attachment")
        self.assertEqual(replace_arguments["attachment_id"], "ATTACHMENT-A")
        self.assertEqual(replace_arguments["url"], new_url)
        self.assertTrue(
            replace_arguments["idempotency_key"].startswith("core-url-replace-")
        )
        self.assertNotIn("UNRELATED", repr(replace_arguments))

    def test_url_patch_preserves_ambiguous_matching_attachments(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "55555555-5555-4555-8555-555555555555",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "A-1", "type": "url", "url": old_url},
                        {"id": "A-2", "type": "url", "url": old_url},
                    ],
                    "truncated": False,
                }
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": new_url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "partial_success")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertEqual(len(adapter_call.call_args_list), 1)
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertTrue(reply.payload["verification"]["final_read"])

    def test_url_patch_reuses_one_existing_target_without_duplicate_write(self) -> None:
        url = "https://example.com/already-visible"
        existing = {"id": "ATTACHMENT-EXISTING", "type": "url", "url": url}
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "66666666-6666-4666-8666-666666666666",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [existing],
                    "truncated": False,
                }
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        def build_argv(tool_name: str, arguments: dict[str, Any]) -> list[str]:
            argv_calls.append((tool_name, copy.deepcopy(arguments)))
            return [tool_name]

        reply = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=build_argv,
        ).invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "unchanged")
        self.assertEqual(reply.payload["after"]["url_attachment"], existing)
        self.assertFalse(
            reply.payload["verification"]["url_attachment"]["write_performed"]
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_native_url_write_promotes_composite_unchanged_to_committed(self) -> None:
        url = "https://example.com/new-visible-url"
        eventkit_receipt = valid_eventkit_receipt(
            "update_reminder",
            status="unchanged",
            target={"id": REMINDER_ID},
            before={"id": REMINDER_ID, "url": url},
            after={"id": REMINDER_ID, "url": url},
        )
        final_read = {
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        attachment = {
            "id": "ATTACHMENT-NEW",
            "type": "url",
            "url": url,
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {"attachment": attachment},
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "not_applicable"},
                    }
                ),
            ]
        )
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, _arguments: [name],
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "verified")
        self.assertTrue(reply.payload["verification"]["write_performed"])
        self.assertEqual(reply.payload["after"]["url_attachment"], attachment)
        self.assertEqual(reply.mutation_state, "committed")

    def test_native_url_commit_survives_failed_composite_final_read(self) -> None:
        url = "https://example.com/new-visible-url"
        eventkit_receipt = valid_eventkit_receipt(
            "update_reminder",
            status="unchanged",
            target={"id": REMINDER_ID},
            before={"id": REMINDER_ID, "url": url},
            after={"id": REMINDER_ID, "url": url},
        )
        bridge_call = mock.Mock(
            side_effect=[
                transport(eventkit_receipt),
                transport(
                    {
                        "ok": False,
                        "error": {
                            "code": "sync_pending",
                            "reason_code": "final_read_lost",
                        },
                    },
                    is_error=True,
                ),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-NEW",
                                "type": "url",
                                "url": url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {"semantics": "not_applicable"},
                    }
                ),
            ]
        )
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, _arguments: [name],
        )

        reply = backend.invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertEqual(reply.payload["status"], "committed_verification_pending")
        self.assertTrue(reply.payload["verification"]["write_performed"])
        self.assertFalse(reply.payload["verification"]["final_read"])
        self.assertEqual(reply.mutation_state, "committed")

    def test_unchanged_url_with_another_url_attachment_fails_closed_without_write(
        self,
    ) -> None:
        url = "https://example.com/already-visible"
        eventkit_receipt = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        bridge_call = mock.Mock(
            side_effect=[transport(eventkit_receipt), transport(final_read)]
        )
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "ATTACHMENT-B", "type": "url", "url": url},
                        {
                            "id": "ATTACHMENT-C",
                            "type": "url",
                            "url": "https://example.com/unrelated",
                        },
                    ],
                    "truncated": False,
                }
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        reply = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        ).invoke(
            "update_reminder",
            {
                "reminder_id": REMINDER_ID,
                "expected_last_modified": "2026-08-25T01:00:00.000Z",
                "patch": {"url": url},
            },
            mutation=True,
        )

        self.assertTrue(reply.is_error)
        self.assertEqual(reply.payload["status"], "failed_no_mutation")
        self.assertEqual(reply.payload["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            reply.payload["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(reply.payload["verification"]["write_performed"])
        self.assertFalse(reply.payload["verification"]["final_read"])
        self.assertFalse(reply.payload["recovery"]["automatic_retry_safe"])
        self.assertEqual(reply.payload["next_action"]["tool"], "read_reminder")
        self.assertFalse(reply.payload["next_action"]["retry_original_once"])
        self.assertIn(
            "inspect_reminder_native",
            reply.payload["recovery"]["manual_action"],
        )
        self.assertIn(
            "change_reminder_attachment",
            reply.payload["recovery"]["manual_action"],
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_fresh_retry_after_partial_url_replace_does_not_hide_a_and_b(
        self,
    ) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"

        def reminder(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "id": REMINDER_ID,
                "title": "URL retry regression",
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": last_modified,
            }

        def read_receipt(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "read_reminder",
                "data": {"reminder": reminder(url, last_modified)},
            }

        first_eventkit_write = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "88888888-8888-4888-8888-888888888888",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(old_url, "2026-08-25T01:00:00.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        retry_eventkit_no_write = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        bridge_call = mock.Mock(
            side_effect=[
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(first_eventkit_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(retry_eventkit_no_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
            ]
        )
        adapter_call = mock.Mock(
            side_effect=[
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 7,
                        "attachments": [
                            {
                                "id": "ATTACHMENT-A",
                                "type": "url",
                                "url": old_url,
                            }
                        ],
                        "truncated": False,
                    }
                ),
                transport(
                    {
                        "ok": False,
                        "status": "failed_manual_repair_required",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_uncertain",
                            "message": "The native replacement outcome is uncertain.",
                        },
                    }
                ),
                transport(
                    {
                        "ok": True,
                        "reminder_id": REMINDER_ID,
                        "reminder_version": 8,
                        "attachments": [
                            {
                                "id": "ATTACHMENT-A",
                                "type": "url",
                                "url": old_url,
                            },
                            {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            },
                        ],
                        "truncated": False,
                    }
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )
        tokens = iter(["A" * 32, "B" * 32])
        facade = V2CoreFacade(backend, token_source=lambda: next(tokens))

        first_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        first = facade.change_reminder(
            {
                "reference": first_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )
        fresh_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        retry = facade.change_reminder(
            {
                "reference": fresh_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )

        self.assertEqual(first["status"], "partial_success")
        self.assertEqual(retry["status"], "failed_no_mutation")
        self.assertFalse(retry["ok"])
        self.assertEqual(retry["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            retry["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(retry["verification"]["write_performed"])
        self.assertFalse(retry["verification"]["final_read"])
        self.assertIsNone(retry["before"])
        self.assertIsNone(retry["after"])
        self.assertNotIn("reference", repr(retry))
        self.assertIn(
            "inspect_reminder_native",
            retry["recovery"]["manual_action"],
        )
        self.assertIn(
            "change_reminder_attachment",
            retry["recovery"]["manual_action"],
        )
        self.assertEqual(
            [name for name, _ in argv_calls],
            [
                "list_reminder_attachments",
                "replace_reminder_attachment",
                "list_reminder_attachments",
            ],
        )

    def test_fresh_retry_with_only_stale_a_performs_no_native_write(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"

        def reminder(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "id": REMINDER_ID,
                "title": "URL A-only retry regression",
                "url": url,
                "calendar_id": "LIST-1",
                "last_modified": last_modified,
            }

        def read_receipt(url: str, last_modified: str) -> dict[str, Any]:
            return {
                "schema_version": 1,
                "ok": True,
                "status": "verified",
                "operation": "read_reminder",
                "data": {"reminder": reminder(url, last_modified)},
            }

        first_eventkit_write = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "88888888-8888-4888-8888-888888888888",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(old_url, "2026-08-25T01:00:00.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        retry_eventkit_no_write = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "99999999-9999-4999-8999-999999999999",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "after": reminder(new_url, "2026-08-25T01:00:01.000Z"),
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        bridge_call = mock.Mock(
            side_effect=[
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(read_receipt(old_url, "2026-08-25T01:00:00.000Z")),
                transport(first_eventkit_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
                transport(retry_eventkit_no_write),
                transport(read_receipt(new_url, "2026-08-25T01:00:01.000Z")),
            ]
        )
        stale_a_inventory = {
            "ok": True,
            "reminder_id": REMINDER_ID,
            "reminder_version": 7,
            "attachments": [
                {"id": "ATTACHMENT-A", "type": "url", "url": old_url}
            ],
            "truncated": False,
        }
        adapter_call = mock.Mock(
            side_effect=[
                transport(copy.deepcopy(stale_a_inventory)),
                transport(
                    {
                        "ok": False,
                        "status": "failed_no_mutation",
                        "operation": "replace_attachment",
                        "error": {
                            "code": "native_url_replace_failed",
                            "message": "The native replacement did not commit.",
                        },
                    }
                ),
                transport(copy.deepcopy(stale_a_inventory)),
                transport(
                    {
                        "ok": True,
                        "status": "verified",
                        "operation": "attach_url",
                        "after": {
                            "attachment": {
                                "id": "ATTACHMENT-B",
                                "type": "url",
                                "url": new_url,
                            }
                        },
                        "verification": {"attachment_active": True},
                        "recovery": {
                            "semantics": "not_applicable",
                            "automatic_retry_safe": True,
                        },
                    }
                ),
            ]
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []
        backend = make_backend(
            bridge_call=bridge_call,
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )
        tokens = iter(["C" * 32, "D" * 32])
        facade = V2CoreFacade(backend, token_source=lambda: next(tokens))

        first_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        first = facade.change_reminder(
            {
                "reference": first_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )
        retry_reference = facade.read_reminder({"reminder_id": REMINDER_ID})[
            "data"
        ]["reminder"]["reference"]
        retry = facade.change_reminder(
            {
                "reference": retry_reference,
                "action": {"kind": "patch", "patch": {"url": new_url}},
            }
        )

        self.assertEqual(first["status"], "partial_success")
        self.assertEqual(retry["status"], "failed_no_mutation")
        self.assertEqual(retry["error"]["code"], "ambiguous_scope")
        self.assertEqual(
            retry["error"]["reason_code"],
            "ambiguous_visible_url_attachment",
        )
        self.assertFalse(retry["verification"]["write_performed"])
        self.assertEqual(
            [name for name, _ in argv_calls],
            [
                "list_reminder_attachments",
                "replace_reminder_attachment",
                "list_reminder_attachments",
            ],
        )
        self.assertEqual(adapter_call.call_count, 3)

    def test_url_retry_preserves_existing_a_and_b_instead_of_duplicating_b(self) -> None:
        old_url = "https://example.com/old"
        new_url = "https://example.com/new"
        payload = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": old_url},
            "after": {"id": REMINDER_ID, "url": new_url},
            "verification": {
                "state": "read_back",
                "write_performed": True,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "eventkit_native_api",
                "automatic_retry_safe": False,
            },
        }
        final_read = {
            "schema_version": 1,
            "ok": True,
            "status": "verified",
            "operation": "read_reminder",
            "data": {
                "reminder": {
                    "id": REMINDER_ID,
                    "url": new_url,
                    "calendar_id": "LIST-1",
                    "last_modified": "2026-08-25T01:00:01.000Z",
                }
            },
        }
        adapter_call = mock.Mock(
            return_value=transport(
                {
                    "ok": True,
                    "reminder_id": REMINDER_ID,
                    "reminder_version": 7,
                    "attachments": [
                        {"id": "ATTACHMENT-A", "type": "url", "url": old_url},
                        {"id": "ATTACHMENT-B", "type": "url", "url": new_url},
                    ],
                    "truncated": False,
                }
            )
        )
        argv_calls: list[tuple[str, dict[str, Any]]] = []

        result = make_backend(
            bridge_call=mock.Mock(return_value=transport(final_read)),
            adapter_call=adapter_call,
            build_adapter_argv=lambda name, arguments: (
                argv_calls.append((name, copy.deepcopy(arguments))) or [name]
            ),
        )._ensure_visible_url_attachment(copy.deepcopy(payload), new_url)

        self.assertEqual(result["status"], "partial_success")
        self.assertEqual(
            result["error"]["reason_code"],
            "target_url_attachment_already_exists",
        )
        self.assertEqual(
            [name for name, _ in argv_calls], ["list_reminder_attachments"]
        )
        self.assertEqual(adapter_call.call_count, 1)

    def test_malformed_url_inventory_never_dispatches_an_attachment_write(self) -> None:
        url = "https://example.com/already-visible"
        base_payload = {
            "schema_version": 1,
            "ok": True,
            "status": "unchanged",
            "operation": "update_reminder",
            "operation_id": "77777777-7777-4777-8777-777777777777",
            "backend": "eventkit_public_sdk",
            "target": {"id": REMINDER_ID, "calendar_id": "LIST-1"},
            "before": {"id": REMINDER_ID, "url": url},
            "after": {"id": REMINDER_ID, "url": url},
            "verification": {
                "state": "read_back",
                "write_performed": False,
                "final_read": True,
                "matched": True,
            },
            "recovery": {
                "semantics": "not_applicable",
                "automatic_retry_safe": True,
            },
        }
        malformed_inventories = (
            {"ok": True, "reminder_id": REMINDER_ID, "reminder_version": 7},
            {
                "ok": True,
                "reminder_id": "WRONG-REMINDER",
                "reminder_version": 7,
                "attachments": [],
                "truncated": False,
            },
            {
                "ok": True,
                "reminder_id": REMINDER_ID,
                "reminder_version": 7,
                "attachments": [{"id": "URL-1", "type": "url"}],
                "truncated": False,
            },
            {
                "ok": True,
                "reminder_id": REMINDER_ID,
                "reminder_version": 7,
                "attachments": [],
            },
        )

        for inventory in malformed_inventories:
            with self.subTest(inventory=inventory):
                adapter_call = mock.Mock(return_value=transport(inventory))
                argv_calls: list[tuple[str, dict[str, Any]]] = []
                result = make_backend(
                    bridge_call=mock.Mock(),
                    adapter_call=adapter_call,
                    build_adapter_argv=lambda name, arguments: (
                        argv_calls.append((name, copy.deepcopy(arguments))) or [name]
                    ),
                )._ensure_visible_url_attachment(copy.deepcopy(base_payload), url)

                self.assertEqual(result["status"], "failed_no_mutation")
                self.assertEqual(result["error"]["code"], "ambiguous_scope")
                self.assertEqual(
                    result["error"]["reason_code"],
                    "native_url_attachment_inventory_invalid",
                )
                self.assertEqual(
                    [name for name, _ in argv_calls],
                    ["list_reminder_attachments"],
                )
                self.assertEqual(adapter_call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
