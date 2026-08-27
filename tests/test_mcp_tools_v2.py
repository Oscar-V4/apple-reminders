from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCHEMA_PATH = PLUGIN_ROOT / "schemas" / "mcp-tools.json"

EXPECTED_TOOLS = [
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "inspect_recently_deleted",
    "recover_deleted_reminder",
    "inspect_reminder_native",
    "ensure_reminder_list",
    "create_reminder_section",
    "organize_reminder",
    "change_reminder_attachment",
    "diagnose_reminders",
]

EXPECTED_ANNOTATIONS = {
    "request_reminders_access": (False, False, True, False),
    "list_reminder_lists": (True, False, True, False),
    "fetch_reminders": (True, False, True, False),
    "read_reminder": (True, False, True, False),
    "create_reminder": (False, False, True, False),
    "change_reminder": (False, True, False, False),
    "delete_reminder": (False, True, True, False),
    "inspect_recently_deleted": (True, False, True, False),
    "recover_deleted_reminder": (False, False, True, False),
    "inspect_reminder_native": (True, False, True, False),
    "ensure_reminder_list": (False, False, True, False),
    "create_reminder_section": (False, False, True, False),
    "organize_reminder": (False, True, False, False),
    "change_reminder_attachment": (False, True, False, False),
    "diagnose_reminders": (True, False, True, False),
}

EXPECTED_TITLES = {
    "request_reminders_access": "Request Reminders access",
    "list_reminder_lists": "List reminder lists",
    "fetch_reminders": "Fetch reminders",
    "read_reminder": "Read reminder",
    "create_reminder": "Create reminder",
    "change_reminder": "Change reminder",
    "delete_reminder": "Delete reminder",
    "inspect_recently_deleted": "Inspect recently deleted reminders",
    "recover_deleted_reminder": "Recover deleted reminder",
    "inspect_reminder_native": "Inspect reminder details",
    "ensure_reminder_list": "Ensure reminder list",
    "create_reminder_section": "Create reminder section",
    "organize_reminder": "Organize reminder",
    "change_reminder_attachment": "Change reminder attachment",
    "diagnose_reminders": "Diagnose Reminders",
}

EXPECTED_REQUIRED = {
    "request_reminders_access": set(),
    "list_reminder_lists": set(),
    "fetch_reminders": set(),
    "read_reminder": {"reminder_id"},
    "create_reminder": {"list_id", "title", "idempotency_key"},
    "change_reminder": {"reference", "action"},
    "delete_reminder": {"reference"},
    "inspect_recently_deleted": {"kind"},
    "recover_deleted_reminder": {"reference", "list_id", "idempotency_key"},
    "inspect_reminder_native": {"kind"},
    "ensure_reminder_list": {"source_id", "name", "idempotency_key"},
    "create_reminder_section": {"list_id", "name"},
    "organize_reminder": {"reference", "action"},
    "change_reminder_attachment": {"reference", "action"},
    "diagnose_reminders": set(),
}

def resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    prefix = "#/$defs/"
    if not reference.startswith(prefix):
        raise AssertionError(f"non-local schema reference: {reference}")
    name = reference.removeprefix(prefix)
    resolved = root.get("$defs", {}).get(name)
    if not isinstance(resolved, dict):
        raise AssertionError(f"unresolved schema reference: {reference}")
    return resolved


def walk_schema(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        if value.get("type") == "object":
            if value.get("additionalProperties") is not False:
                errors.append(f"{path}: object schema is not closed")
        expected = value.get("type")
        string_typed = expected == "string" or (
            isinstance(expected, list) and "string" in expected
        )
        if string_typed and "const" not in value and "enum" not in value:
            if "maxLength" not in value:
                errors.append(f"{path}: string schema is unbounded")
        if expected == "array" and "maxItems" not in value:
            errors.append(f"{path}: array schema is unbounded")
        if "$ref" in value and not str(value["$ref"]).startswith("#/$defs/"):
            errors.append(f"{path}: reference is not self-contained")
        for name, item in value.items():
            errors.extend(walk_schema(item, path=f"{path}.{name}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(walk_schema(item, path=f"{path}[{index}]"))
    return errors


class McpToolsV2SchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.tools = cls.document["tools"]
        cls.by_name = {tool["name"]: tool for tool in cls.tools}

    def test_public_surface_exposes_exactly_the_agreed_fifteen_tools(self) -> None:
        self.assertEqual(self.document["schemaVersion"], 2)
        self.assertEqual(set(self.by_name), set(EXPECTED_TOOLS))
        self.assertEqual(len(self.by_name), 15)

    def test_priority_schema_explains_eventkit_order(self) -> None:
        create = self.by_name["create_reminder"]["inputSchema"]["properties"][
            "priority"
        ]["description"]
        patch = self.by_name["change_reminder"]["inputSchema"]["$defs"]["patch"][
            "properties"
        ]["priority"]["description"]

        for description in (create, patch):
            self.assertIn("1-4 high", description)
            self.assertIn("5 medium", description)
            self.assertIn("6-9 low", description)
            self.assertIn("1, 5, or 9", description)

    def test_every_public_tool_has_a_human_readable_title(self) -> None:
        self.assertEqual(
            {name: tool.get("title") for name, tool in self.by_name.items()},
            EXPECTED_TITLES,
        )

    def test_tool_discovery_contract_stays_within_context_budget(self) -> None:
        compact = json.dumps(
            self.document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLessEqual(
            len(compact),
            32_768,
            "The default tool contract is too large for a lightweight public plugin; "
            "deepen or narrow schemas before raising this budget.",
        )

    def test_output_schemas_are_not_duplicated_into_tool_discovery(self) -> None:
        self.assertTrue(all("outputSchema" not in tool for tool in self.tools))

    def test_schema_has_no_duplicate_json_keys(self) -> None:
        def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        json.loads(
            SCHEMA_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
        )

    def test_public_contract_uses_reminder_list_vocabulary(self) -> None:
        encoded = json.dumps(self.document, sort_keys=True)
        self.assertNotIn("calendar_id", encoded)
        self.assertNotIn("calendar_ids", encoded)
        self.assertNotIn("calendar_title", encoded)
        self.assertNotIn("reminder_calendar_count", encoded)

    def test_ensure_list_matches_exact_eventkit_capability(self) -> None:
        tool = self.by_name["ensure_reminder_list"]
        self.assertEqual(
            set(tool["inputSchema"]["properties"]),
            {"source_id", "name", "idempotency_key"},
        )

    def test_fetch_schema_advertises_status_specific_memory_bounds(self) -> None:
        schema = self.by_name["fetch_reminders"]["inputSchema"]

        self.assertEqual(
            schema["properties"]["status"]["enum"],
            ["incomplete", "completed"],
        )
        self.assertEqual(
            schema["oneOf"],
            [
                {
                    "properties": {"status": {"const": "incomplete"}},
                    "anyOf": [
                        {"required": ["list_ids"]},
                        {"required": ["due_start", "due_end"]},
                    ],
                },
                {
                    "required": [
                        "status",
                        "completion_start",
                        "completion_end",
                    ],
                    "properties": {"status": {"const": "completed"}},
                },
            ],
        )
        self.assertEqual(
            schema["x-rangeConstraints"],
            [
                {
                    "start": "due_start",
                    "end": "due_end",
                    "maximumDays": 366,
                },
                {
                    "start": "completion_start",
                    "end": "completion_end",
                    "maximumDays": 90,
                },
            ],
        )

    def test_doctor_does_not_advertise_withheld_maintenance_workflows(self) -> None:
        scope = self.by_name["diagnose_reminders"]["inputSchema"]["properties"][
            "scope"
        ]["enum"]
        self.assertNotIn("maintenance", scope)
        self.assertNotIn("snapshots", scope)

    def test_every_tool_has_a_self_contained_closed_bounded_input_schema(self) -> None:
        for tool in self.tools:
            with self.subTest(tool=tool["name"]):
                self.assertIsInstance(tool.get("description"), str)
                self.assertTrue(tool["description"].strip())
                schema = tool["inputSchema"]
                self.assertEqual(schema.get("type"), "object")
                self.assertIs(schema.get("additionalProperties"), False)
                errors = walk_schema(
                    schema,
                    path=f"{tool['name']}.inputSchema",
                )
                self.assertEqual(errors, [])
                for reference in re.findall(
                    r'"\$ref"\s*:\s*"([^"]+)"',
                    json.dumps(schema),
                ):
                    resolve_ref(schema, reference)

    def test_required_fields_and_annotations_are_exact(self) -> None:
        annotation_keys = (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        )
        for name, expected in EXPECTED_ANNOTATIONS.items():
            with self.subTest(tool=name):
                tool = self.by_name[name]
                self.assertEqual(set(tool["annotations"]), set(annotation_keys))
                self.assertEqual(
                    tuple(tool["annotations"][key] for key in annotation_keys),
                    expected,
                )
        for name, expected in EXPECTED_REQUIRED.items():
            with self.subTest(tool=name):
                self.assertEqual(
                    set(self.by_name[name]["inputSchema"].get("required", [])),
                    expected,
                )

    def test_reference_is_one_opaque_rev1_token(self) -> None:
        for name in (
            "read_reminder",
            "create_reminder",
            "change_reminder",
            "delete_reminder",
            "inspect_reminder_native",
            "organize_reminder",
            "change_reminder_attachment",
        ):
            encoded = json.dumps(self.by_name[name]["inputSchema"], sort_keys=True)
            if "reference" not in encoded:
                continue
            self.assertIn("^rev1", encoded, name)
            self.assertNotIn('"store_id"', encoded, name)
            self.assertNotIn('"revision"', encoded, name)

    def test_copy_image_is_a_closed_two_reference_action_without_a_file_path(self) -> None:
        schema = self.by_name["change_reminder_attachment"]["inputSchema"]
        actions = schema["properties"]["action"]["oneOf"]
        copy_action = next(
            action
            for action in actions
            if action["properties"]["kind"].get("const") == "copy_image"
        )

        self.assertEqual(
            set(copy_action["properties"]),
            {"kind", "source_reference", "attachment_id", "idempotency_key"},
        )
        self.assertEqual(
            set(copy_action["required"]),
            {"kind", "source_reference", "attachment_id", "idempotency_key"},
        )
        self.assertIs(copy_action["additionalProperties"], False)
        self.assertNotIn("image_path", json.dumps(copy_action))
        reference = resolve_ref(schema, copy_action["properties"]["source_reference"]["$ref"])
        self.assertEqual(reference["pattern"], "^rev1\\.[A-Za-z0-9_-]{32,4091}$")


if __name__ == "__main__":
    unittest.main()
